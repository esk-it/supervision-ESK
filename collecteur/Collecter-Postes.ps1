#Requires -Version 7.0
#Requires -Modules ActiveDirectory
<#
.SYNOPSIS
    Collecteur sans agent : interroge les postes du domaine et envoie leur état
    au serveur de supervision.

.DESCRIPTION
    Toutes les N secondes, pour chaque ordinateur des OU configurées :
      1. teste le port WinRM (5985). Pas de réponse : « hors_ligne » ;
      2. ouvre une session CIM (WinRM) et lit l'utilisateur de la console
         (Win32_ComputerSystem.UserName) et la présence de LogonUI.exe ;
           - utilisateur + LogonUI.exe   : « verrouille »
           - utilisateur sans LogonUI    : « utilise »
           - pas d'utilisateur console mais un explorer.exe : « deconnecte »
             (session ouverte en arrière-plan ou en bureau à distance)
           - personne                    : « libre »
         Échec WinRM (accès refusé, service arrêté…) : « erreur » ;
      3. en option, lit le temps d'inactivité avec quser ;
      4. complète avec le nom complet et le service de l'utilisateur (AD).
    Rien n'est installé sur les postes.

.PARAMETER Config
    Chemin du fichier de configuration JSON (voir config.exemple.json).

.PARAMETER UneFois
    Fait un seul passage puis s'arrête (utile pour tester).

.PARAMETER Afficher
    Affiche le résultat dans la console au lieu de l'envoyer au serveur.

.EXAMPLE
    .\Collecter-Postes.ps1 -UneFois -Afficher
#>
[CmdletBinding()]
param(
    [string]$Config = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$UneFois,
    [switch]$Afficher
)

$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory -Verbose:$false

$cfg = Get-Content -LiteralPath $Config -Raw -Encoding utf8 | ConvertFrom-Json
$intervalle  = [int]($cfg.IntervalleSecondes ?? 30)
$rafraichAD  = [int]($cfg.RafraichirADMinutes ?? 15)
$parallele   = [int]($cfg.Parallelisme ?? 64)
$delai       = [int]($cfg.DelaiSecondes ?? 5)
$avecQuser   = [bool]($cfg.Quser ?? $true)
$filtreAD    = [string]($cfg.FiltreAD ?? "Enabled -eq 'True'")
$urlReleve   = ([string]$cfg.ServeurUrl).TrimEnd('/') + '/api/releve'

function Write-Journal([string]$Message, [string]$Niveau = 'INFO') {
    Write-Host ("{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f (Get-Date), $Niveau, $Message)
}

function Get-OrdinateursAD {
    foreach ($ou in $cfg.OUs) {
        Get-ADComputer -SearchBase $ou -Filter $filtreAD -Properties DNSHostName |
            ForEach-Object {
                # Groupe affiché = première OU au-dessus de l'objet ordinateur.
                $ous = [regex]::Matches($_.DistinguishedName, '(?<=OU=)(?:[^,\\]|\\.)+') |
                    ForEach-Object Value
                [pscustomobject]@{
                    Nom    = $_.Name
                    Hote   = $_.DNSHostName ? $_.DNSHostName : $_.Name
                    Groupe = $ous | Select-Object -First 1
                    OU     = ($_.DistinguishedName -replace '^CN=(?:[^,\\]|\\.)+,', '')
                }
            }
    }
}

# Cache des utilisateurs AD (nom complet, service), rafraîchi toutes les heures.
$cacheUtilisateurs = @{}
function Get-InfosUtilisateur([string]$Compte) {
    $login = $Compte.Split('\')[-1]
    $entree = $cacheUtilisateurs[$login]
    if ($entree -and $entree.Expire -gt (Get-Date)) { return $entree }
    $infos = [pscustomobject]@{ NomComplet = $null; Service = $null; Expire = (Get-Date).AddHours(1) }
    try {
        $u = Get-ADUser -Identity $login -Properties DisplayName, Department
        $infos.NomComplet = $u.DisplayName
        $infos.Service = $u.Department
    } catch {
        # Compte local ou d'un autre domaine : on garde juste le login.
    }
    $cacheUtilisateurs[$login] = $infos
    return $infos
}

# Interrogation d'un poste. Exécuté en parallèle : ne dépend que de ses paramètres.
$sondePoste = {
    param($Poste, [int]$Delai, [bool]$AvecQuser)

    $r = [ordered]@{
        nom = $Poste.Nom; groupe = $Poste.Groupe; ou = $Poste.OU; ip = $null
        etat = 'hors_ligne'; utilisateur = $null; inactif_min = $null
        demarrage = $null; erreur = $null
    }

    # 1. Le poste répond-il sur WinRM ?
    $tcp = [System.Net.Sockets.TcpClient]::new()
    try {
        if (-not $tcp.ConnectAsync($Poste.Hote, 5985).Wait($Delai * 1000)) { return [pscustomobject]$r }
        $r.ip = $tcp.Client.RemoteEndPoint.Address.MapToIPv4().ToString()
    } catch {
        return [pscustomobject]$r   # nom inconnu du DNS, port fermé… : éteint
    } finally {
        $tcp.Dispose()
    }

    # 2. Session CIM sur WinRM.
    $session = $null
    try {
        $session = New-CimSession -ComputerName $Poste.Hote -OperationTimeoutSec $Delai `
            -SessionOption (New-CimSessionOption -Protocol Wsman) -ErrorAction Stop
        $cs = Get-CimInstance -CimSession $session -ClassName Win32_ComputerSystem -Property UserName
        $os = Get-CimInstance -CimSession $session -ClassName Win32_OperatingSystem -Property LastBootUpTime
        $verrou = Get-CimInstance -CimSession $session -ClassName Win32_Process `
            -Filter "Name='LogonUI.exe'" -Property ProcessId
        if ($os.LastBootUpTime) { $r.demarrage = $os.LastBootUpTime.ToString('dd/MM/yyyy HH:mm') }

        if ($cs.UserName) {
            $r.utilisateur = $cs.UserName
            $r.etat = $verrou ? 'verrouille' : 'utilise'
        } else {
            # Pas de session console : une session ouverte ailleurs (RDP,
            # changement d'utilisateur) se voit à son explorer.exe.
            $explorer = Get-CimInstance -CimSession $session -ClassName Win32_Process `
                -Filter "Name='explorer.exe'" | Select-Object -First 1
            if ($explorer) {
                $proprio = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
                if ($proprio.User) {
                    $r.utilisateur = "$($proprio.Domain)\$($proprio.User)"
                    $r.etat = 'deconnecte'
                } else { $r.etat = 'libre' }
            } else { $r.etat = 'libre' }
        }
    } catch {
        $r.etat = 'erreur'
        $r.erreur = ($_.Exception.Message -split "`n")[0].Trim()
        return [pscustomobject]$r
    } finally {
        if ($session) { Remove-CimSession -CimSession $session -ErrorAction SilentlyContinue }
    }

    # 3. Temps d'inactivité (facultatif, demande « AllowRemoteRPC » sur les postes).
    if ($AvecQuser -and $r.utilisateur) {
        try {
            $login = $r.utilisateur.Split('\')[-1]
            $lignes = & quser.exe "/server:$($Poste.Hote)" 2>$null | Select-Object -Skip 1
            foreach ($ligne in $lignes) {
                # Colonnes séparées par 2 espaces ou plus ; on lit depuis la fin car
                # le nom de session est vide pour une session déconnectée.
                # Indépendant de la langue de Windows.
                $c = ($ligne.Trim().TrimStart('>')) -split '\s{2,}'
                if ($c.Count -lt 5 -or $c[0] -ne $login) { continue }
                $txt = $c[-2]
                $min = switch -Regex ($txt) {
                    '^(\d+)\+(\d+):(\d+)$' { [int]$Matches[1] * 1440 + [int]$Matches[2] * 60 + [int]$Matches[3]; break }
                    '^(\d+):(\d+)$'         { [int]$Matches[1] * 60 + [int]$Matches[2]; break }
                    '^(\d+)$'               { [int]$Matches[1]; break }
                    default                 { 0 }   # « . », « aucun », « none »
                }
                if ($null -eq $r.inactif_min -or $min -lt $r.inactif_min) { $r.inactif_min = $min }
            }
        } catch { }
    }

    [pscustomobject]$r
}

$ordinateurs = @()
$dernierAD = [datetime]::MinValue
$codeSonde = $sondePoste.ToString()

Write-Journal "Démarrage du collecteur (intervalle $intervalle s, $parallele en parallèle)"
while ($true) {
    $debut = Get-Date
    try {
        if ($debut - $dernierAD -gt [timespan]::FromMinutes($rafraichAD)) {
            $ordinateurs = @(Get-OrdinateursAD)
            $dernierAD = $debut
            Write-Journal "$($ordinateurs.Count) ordinateurs trouvés dans l'AD"
        }

        $resultats = $ordinateurs | ForEach-Object -ThrottleLimit $parallele -Parallel {
            $sonde = [scriptblock]::Create($using:codeSonde)
            & $sonde $_ $using:delai $using:avecQuser
        }

        foreach ($r in $resultats) {
            if ($r.utilisateur) {
                $infos = Get-InfosUtilisateur $r.utilisateur
                $r | Add-Member nom_complet $infos.NomComplet
                $r | Add-Member service $infos.Service
            }
        }

        if ($Afficher) {
            $resultats | Sort-Object nom | Format-Table nom, etat, utilisateur, nom_complet, inactif_min, ip, erreur -AutoSize
        } else {
            $json = @{ postes = @($resultats) } | ConvertTo-Json -Depth 4 -Compress
            $rep = Invoke-RestMethod -Method Post -Uri $urlReleve -Headers @{ 'X-Token' = $cfg.Token } `
                -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json)) `
                -TimeoutSec 30
            $stats = $resultats | Group-Object etat | ForEach-Object { "$($_.Name)=$($_.Count)" }
            Write-Journal ("{0} postes en {1:N1} s ({2}), {3} changements envoyés" -f `
                $resultats.Count, ((Get-Date) - $debut).TotalSeconds, ($stats -join ' '), $rep.modifies)
        }
    } catch {
        Write-Journal "Passage en échec : $($_.Exception.Message)" 'ERREUR'
    }

    if ($UneFois) { break }
    $reste = $intervalle - ((Get-Date) - $debut).TotalSeconds
    if ($reste -gt 0) { Start-Sleep -Seconds ([math]::Ceiling($reste)) }
}
