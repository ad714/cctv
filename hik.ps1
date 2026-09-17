param(
    [Parameter(Position = 0)]
    [ValidateSet('info', 'check', 'snapshot', 'record', 'watch', 'view', 'stream-url', 'find', 'clip', 'open', 'help')]
    [string]$Command = 'help',

    [string]$Ip = '192.168.1.6',
    [int]$Channel = 101,
    [int]$Seconds = 60,
    [int]$Interval = 3,
    [string]$Webhook = '',
    [string]$Start = '',
    [string]$End = '',
    [string]$OutDir = "$PSScriptRoot\captures"
)

$ErrorActionPreference = 'Stop'

function Get-HikCred {
    if (-not $script:HikCred) {
        $envFile = Join-Path $PSScriptRoot '.hikenv'
        if (Test-Path $envFile) {
            $kv = @{}
            foreach ($line in Get-Content $envFile) {
                if ($line -match '^\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$') { $kv[$Matches[1]] = $Matches[2].Trim('"').Trim("'") }
            }
            if ($kv['HIK_PASS']) {
                $u = if ($kv['HIK_USER']) { $kv['HIK_USER'] } else { 'admin' }
                $script:HikCred = New-Object System.Management.Automation.PSCredential($u, (ConvertTo-SecureString $kv['HIK_PASS'] -AsPlainText -Force))
                return $script:HikCred
            }
        }
        $user = Read-Host "Hikvision username for $Ip (press Enter for 'admin')"
        if ([string]::IsNullOrWhiteSpace($user)) { $user = 'admin' }
        $sec = Read-Host "Password for $user@$Ip" -AsSecureString
        if (-not $sec -or $sec.Length -eq 0) { throw 'No password entered - aborting.' }
        $script:HikCred = New-Object System.Management.Automation.PSCredential($user, $sec)
    }
    return $script:HikCred
}

function ConvertTo-Md5Hex {
    param([string]$Text)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $hash = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text))
    return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-DigestHeader {
    param([string]$WwwAuth, [string]$User, [string]$Pass, [string]$Method, [string]$Uri)
    $f = @{}
    foreach ($m in [regex]::Matches($WwwAuth, '(\w+)=(?:"([^"]*)"|([^,\s]+))')) {
        $val = if ($m.Groups[2].Success) { $m.Groups[2].Value } else { $m.Groups[3].Value }
        $f[$m.Groups[1].Value] = $val
    }
    $realm = $f['realm']; $nonce = $f['nonce']; $qop = $f['qop']; $opaque = $f['opaque']
    $ha1 = ConvertTo-Md5Hex ('{0}:{1}:{2}' -f $User, $realm, $Pass)
    $ha2 = ConvertTo-Md5Hex ('{0}:{1}' -f $Method, $Uri)
    if ($qop) {
        $nc = '00000001'
        $cnonce = [guid]::NewGuid().ToString('N').Substring(0, 16)
        $resp = ConvertTo-Md5Hex ('{0}:{1}:{2}:{3}:{4}:{5}' -f $ha1, $nonce, $nc, $cnonce, $qop, $ha2)
        $h = 'Digest username="{0}", realm="{1}", nonce="{2}", uri="{3}", qop={4}, nc={5}, cnonce="{6}", response="{7}", algorithm=MD5' -f $User, $realm, $nonce, $Uri, $qop, $nc, $cnonce, $resp
    } else {
        $resp = ConvertTo-Md5Hex ('{0}:{1}:{2}' -f $ha1, $nonce, $ha2)
        $h = 'Digest username="{0}", realm="{1}", nonce="{2}", uri="{3}", response="{4}", algorithm=MD5' -f $User, $realm, $nonce, $Uri, $resp
    }
    if ($opaque) { $h += ', opaque="{0}"' -f $opaque }
    return $h
}

function Get-HikAuth {
    param([string]$Path, [string]$Method)
    $cred = Get-HikCred
    $req = [System.Net.HttpWebRequest]::Create("http://$Ip$Path")
    $req.Method = $Method
    $req.Timeout = 20000
    try {
        $req.GetResponse().Close()
        return $null
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r -and [int]$r.StatusCode -eq 401) {
            $www = $r.Headers['WWW-Authenticate']
            $r.Close()
            return Get-DigestHeader -WwwAuth $www -User $cred.UserName -Pass $cred.GetNetworkCredential().Password -Method $Method -Uri $Path
        }
        throw
    }
}

function Invoke-Hik {
    param(
        [string]$Path,
        [string]$Method = 'GET',
        [string]$OutFile,
        [string]$Body
    )
    $auth = Get-HikAuth -Path $Path -Method $Method
    $req = [System.Net.HttpWebRequest]::Create("http://$Ip$Path")
    $req.Method = $Method
    $req.Timeout = 20000
    if ($auth) { $req.Headers.Add('Authorization', $auth) }
    if ($Body) {
        $req.ContentType = 'application/xml'
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
        $req.ContentLength = $bytes.Length
        $rs = $req.GetRequestStream(); $rs.Write($bytes, 0, $bytes.Length); $rs.Close()
    }
    try {
        $resp = $req.GetResponse()
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r -and [int]$r.StatusCode -eq 401) {
            throw "401 Unauthorized - check the username/password for $Ip."
        }
        if ($r) {
            $er = New-Object System.IO.StreamReader($r.GetResponseStream())
            $detail = $er.ReadToEnd(); $er.Close(); $r.Close()
            throw ("{0} from {1}`n{2}" -f [int]$r.StatusCode, $Path, $detail)
        }
        throw
    }
    $stream = $resp.GetResponseStream()
    if ($OutFile) {
        $fs = [System.IO.File]::Create($OutFile)
        $stream.CopyTo($fs); $fs.Close(); $resp.Close()
        return
    }
    $sr = New-Object System.IO.StreamReader($stream)
    $text = $sr.ReadToEnd(); $sr.Close(); $resp.Close()
    return [pscustomobject]@{ Content = $text }
}

function Get-HikXml {
    param([string]$Path)
    $resp = Invoke-Hik -Path $Path
    return [xml]$resp.Content
}

function Show-Value {
    param([string]$Label, $Value)
    "{0,-26} {1}" -f ($Label + ':'), $Value | Write-Host
}

function Cmd-Info {
    Write-Host "`n=== Device Info ($Ip) ===" -ForegroundColor Cyan
    $x = Get-HikXml '/ISAPI/System/deviceInfo'
    $d = $x.DeviceInfo
    Show-Value 'Name'        $d.deviceName
    Show-Value 'Model'       $d.model
    Show-Value 'Type'        $d.deviceType
    Show-Value 'Serial'      $d.serialNumber
    Show-Value 'Firmware'    ("{0} ({1})" -f $d.firmwareVersion, $d.firmwareReleasedDate)
    Show-Value 'MAC'         $d.macAddress

    try {
        $ch = Get-HikXml '/ISAPI/System/Video/inputs/channels'
        $count = @($ch.VideoInputChannelList.VideoInputChannel).Count
        Write-Host "`nVideo input channels: $count" -ForegroundColor Cyan
        foreach ($c in $ch.VideoInputChannelList.VideoInputChannel) {
            Show-Value ("  ch " + $c.id) $c.name
        }
    } catch { }
}

function Cmd-Check {
    Write-Host "`n=== Security Hardening Check ($Ip) ===" -ForegroundColor Cyan

    try {
        $x = Get-HikXml '/ISAPI/System/deviceInfo'
        Show-Value 'Firmware' ("{0} ({1})" -f $x.DeviceInfo.firmwareVersion, $x.DeviceInfo.firmwareReleasedDate)
        Write-Host '   -> Compare against latest on hikvision.com/support; update if older than ~1 year.' -ForegroundColor DarkGray
    } catch { Write-Host "deviceInfo failed: $($_.Exception.Message)" -ForegroundColor Yellow }

    try {
        $u = Get-HikXml '/ISAPI/Security/users'
        Write-Host "`nUser accounts:" -ForegroundColor Cyan
        foreach ($usr in $u.UserList.User) {
            Show-Value ("  " + $usr.userName) ("level=" + $usr.userLevel)
        }
    } catch { Write-Host "users lookup failed: $($_.Exception.Message)" -ForegroundColor Yellow }

    try {
        $p = Get-HikXml '/ISAPI/System/Network/PlatformAccess'
        $enabled = $p.PlatformAccess.enabled
        Write-Host "`nHik-Connect / cloud (PlatformAccess):" -ForegroundColor Cyan
        Show-Value '  Enabled' $enabled
        Write-Host '   -> If you do not use remote viewing, disabling this reduces exposure.' -ForegroundColor DarkGray
    } catch { Write-Host "PlatformAccess lookup: $($_.Exception.Message)" -ForegroundColor DarkGray }

    foreach ($svc in @(
            @{ Name = 'UPnP'; Path = '/ISAPI/System/Network/UPnP'; Node = 'UPnP'; Field = 'enabled' },
            @{ Name = 'SSH';  Path = '/ISAPI/Security/adminAccesses'; Node = 'AdminAccessProtocolList'; Field = '' }
        )) {
        try {
            $r = Get-HikXml $svc.Path
            Write-Host "`n$($svc.Name):" -ForegroundColor Cyan
            Write-Host ("   " + $r.OuterXml.Substring(0, [Math]::Min(300, $r.OuterXml.Length))) -ForegroundColor DarkGray
        } catch { }
    }

    Write-Host "`nInternet-exposure note:" -ForegroundColor Cyan
    Write-Host '   Check your router for any port-forwarding to 192.168.1.4 (ports 80/554/8000).' -ForegroundColor DarkGray
    Write-Host '   A camera reachable from the public internet is the #1 Hikvision risk.' -ForegroundColor DarkGray
}

function Cmd-Snapshot {
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $file = Join-Path $OutDir "snap-ch$Channel-$stamp.jpg"
    Invoke-Hik -Path "/ISAPI/Streaming/channels/$Channel/picture" -OutFile $file | Out-Null
    Write-Host "Saved snapshot: $file" -ForegroundColor Green
}

function Get-RtspUrl {
    $cred = Get-HikCred
    $u = $cred.UserName
    $p = $cred.GetNetworkCredential().Password
    return "rtsp://${u}:${p}@${Ip}:554/Streaming/Channels/$Channel"
}

function Cmd-StreamUrl {
    $cred = Get-HikCred
    $masked = "rtsp://$($cred.UserName):********@${Ip}:554/Streaming/Channels/$Channel"
    Write-Host "`nRTSP URL (paste into VLC -> Media -> Open Network Stream):" -ForegroundColor Cyan
    Write-Host "  $masked"
    Write-Host "`nMain stream = $Channel (e.g. 101). Sub/low-res stream = 102." -ForegroundColor DarkGray
    Set-Clipboard -Value (Get-RtspUrl)
    Write-Host 'Full URL (with password) copied to clipboard.' -ForegroundColor Green
}

function Cmd-Record {
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff) {
        Write-Host 'ffmpeg not found. Install it, then re-run:' -ForegroundColor Yellow
        Write-Host '  winget install Gyan.FFmpeg' -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $file = Join-Path $OutDir "rec-ch$Channel-$stamp.mp4"
    $url = Get-RtspUrl
    Write-Host "Recording $Seconds s from channel $Channel -> $file" -ForegroundColor Green
    & ffmpeg -loglevel warning -rtsp_transport tcp -i $url -t $Seconds -c copy $file
    Write-Host "Done: $file" -ForegroundColor Green
}

function Cmd-Watch {
    $path = '/ISAPI/Event/notification/alertStream'
    $auth = Get-HikAuth -Path $path -Method 'GET'
    $req = [System.Net.HttpWebRequest]::Create("http://$Ip$path")
    if ($auth) { $req.Headers.Add('Authorization', $auth) }
    $req.Timeout = 15000
    $req.ReadWriteTimeout = [int]::MaxValue
    Write-Host "Listening for motion/events on $Ip ... press Ctrl+C to stop.`n" -ForegroundColor Cyan
    $resp = $req.GetResponse()
    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
    $etype = ''
    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if ($line -match '<eventType>(.+?)</eventType>') { $etype = $Matches[1] }
        if ($line -match '<eventState>(.+?)</eventState>') {
            $state = $Matches[1]
            if ($state -eq 'active' -and $etype -match 'VMD|linedetection|fielddetection|regionEntrance|regionExiting') {
                $t = (Get-Date).ToString('HH:mm:ss')
                Write-Host "[$t] MOTION: $etype" -ForegroundColor Magenta
                if ($Webhook) {
                    try { Invoke-RestMethod -Uri $Webhook -Method Post -Body (@{ text = "Motion ($etype) on $Ip at $t" } | ConvertTo-Json) -ContentType 'application/json' -TimeoutSec 5 } catch { }
                }
            }
        }
    }
}

function Cmd-View {
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $img = Join-Path $OutDir 'live.jpg'
    $html = Join-Path $OutDir 'viewer.html'
    @"
<!doctype html><meta charset=utf-8><title>Hik ch$Channel</title>
<body style="margin:0;background:#111;display:flex;justify-content:center;align-items:center;height:100vh">
<img id=v style="max-width:100%;max-height:100%">
<script>
setInterval(()=>{document.getElementById('v').src='live.jpg?t='+Date.now()},$($Interval*1000));
</script>
"@ | Set-Content -Path $html -Encoding UTF8
    Start-Process $html
    Write-Host "Viewer open. Refreshing snapshot every $Interval s (Ctrl+C to stop)." -ForegroundColor Green
    while ($true) {
        try { Invoke-Hik -Path "/ISAPI/Streaming/channels/$Channel/picture" -OutFile $img | Out-Null } catch { }
        Start-Sleep -Seconds $Interval
    }
}

function Get-HikLocal {
    param([string]$Text, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Text)) { throw "-$Label is required, e.g. -$Label '2026-09-09 14:30'" }
    return [datetime]::Parse($Text, [Globalization.CultureInfo]::CurrentCulture)
}

function Get-PlaybackUrl {
    param([string]$StartUtc, [string]$EndUtc)
    $cred = Get-HikCred
    $u = [uri]::EscapeDataString($cred.UserName)
    $p = [uri]::EscapeDataString($cred.GetNetworkCredential().Password)
    return "rtsp://${u}:${p}@${Ip}:554/Streaming/tracks/$Channel`?starttime=$StartUtc&endtime=$EndUtc"
}

function Cmd-Find {
    $su = Get-HikLocal -Text $Start -Label 'Start'
    $eu = Get-HikLocal -Text $End -Label 'End'
    $s = $su.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $e = $eu.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $body = @"
<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
  <searchID>$([guid]::NewGuid().ToString())</searchID>
  <trackIDList><trackID>$Channel</trackID></trackIDList>
  <timeSpanList><timeSpan><startTime>$s</startTime><endTime>$e</endTime></timeSpan></timeSpanList>
  <maxResults>50</maxResults>
  <searchResultPostion>0</searchResultPostion>
  <metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>
"@
    $resp = Invoke-Hik -Path '/ISAPI/ContentMgmt/search' -Method 'POST' -Body $body
    $x = [xml]$resp.Content
    $items = @($x.CMSearchResult.matchList.searchMatchItem)
    Write-Host "`nRecordings on channel $Channel between $s and $e (DVR local time)" -ForegroundColor Cyan
    Write-Host ("Status: {0}   Matches: {1}" -f $x.CMSearchResult.responseStatusStrg, $items.Count) -ForegroundColor DarkGray
    foreach ($i in $items) {
        if (-not $i) { continue }
        $st = [datetime]::Parse($i.timeSpan.startTime.TrimEnd('Z'), [Globalization.CultureInfo]::InvariantCulture)
        $en = [datetime]::Parse($i.timeSpan.endTime.TrimEnd('Z'), [Globalization.CultureInfo]::InvariantCulture)
        "  {0:yyyy-MM-dd HH:mm:ss} -> {1:HH:mm:ss}   {2}" -f $st, $en, $i.mediaSegmentDescriptor.contentType | Write-Host
    }
    if ($items.Count -eq 0) { Write-Host '  (nothing recorded in that window)' -ForegroundColor Yellow }
}

function Cmd-Clip {
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff) {
        Write-Host 'ffmpeg not found. Install it, then re-run:' -ForegroundColor Yellow
        Write-Host '  winget install Gyan.FFmpeg' -ForegroundColor Yellow
        return
    }
    $su = Get-HikLocal -Text $Start -Label 'Start'
    $eu = Get-HikLocal -Text $End -Label 'End'
    if ($eu -le $su) { throw '-End must be later than -Start.' }
    $dur = [int]($eu - $su).TotalSeconds
    $s = $su.ToString('yyyyMMddTHHmmssZ')
    $e = $eu.ToString('yyyyMMddTHHmmssZ')
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $file = Join-Path $OutDir ("clip-ch{0}-{1}-{2}.mp4" -f $Channel, $s, $e)
    $url = Get-PlaybackUrl -StartUtc $s -EndUtc $e
    Write-Host ("Pulling channel {0}  {1} -> {2} (DVR local, {3}s)" -f $Channel, $s, $e, $dur) -ForegroundColor Green
    & ffmpeg -y -loglevel warning -rtsp_transport tcp -timeout 15000000 -i $url -t $dur -c:v copy -c:a aac -b:a 64k -movflags +frag_keyframe+empty_moov $file
    $len = if (Test-Path $file) { (Get-Item $file).Length } else { 0 }
    if ($len -gt 0) {
        Write-Host ("Done: {0}  ({1} MB)" -f $file, [math]::Round($len / 1MB, 1)) -ForegroundColor Green
    } else {
        if (Test-Path $file) { Remove-Item $file -Force }
        Write-Host 'Nothing downloaded - run "find" to confirm footage exists for that window.' -ForegroundColor Yellow
    }
}

function Cmd-Open { Start-Process "http://$Ip" }

function Cmd-Help {
    Write-Host @"

Hikvision toolkit  ($Ip)
  Usage:  .\hik.ps1 <command> [-Channel 101] [-Seconds 60] [-Interval 3] [-Webhook <url>]

  info         Show model, firmware, channels
  check        Security / hardening review
  snapshot     Save one JPEG to .\captures
  record       Record <Seconds> of video (needs ffmpeg) to .\captures
  watch        Live motion/event alerts (optional -Webhook to notify)
  view         Auto-refreshing snapshot viewer in your browser
  stream-url   Print + copy the RTSP URL for VLC
  find         List recorded segments in a window   -Start '2026-09-09 14:00' -End '2026-09-09 15:00'
  clip         Download that window to .\captures as MP4 (needs ffmpeg)
  open         Open the camera's native web UI

  Channel numbering:  camera ch1 main = 101, sub = 102.  NVR ch2 main = 201, etc.
"@
}

switch ($Command) {
    'info'       { Cmd-Info }
    'check'      { Cmd-Check }
    'snapshot'   { Cmd-Snapshot }
    'record'     { Cmd-Record }
    'watch'      { Cmd-Watch }
    'view'       { Cmd-View }
    'stream-url' { Cmd-StreamUrl }
    'find'       { Cmd-Find }
    'clip'       { Cmd-Clip }
    'open'       { Cmd-Open }
    default      { Cmd-Help }
}
