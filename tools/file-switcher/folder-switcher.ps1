#requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet('Status','Scan','Capture','Diff','Apply','Rollback')][string]$Action='Status',
  [string]$Config,
  [string]$ProfileId,
  [string]$StateId,
  [string]$SourceRoot,
  [switch]$DryRun,
  [switch]$AllowUnknown,
  [switch]$AllowManagedDelete,
  [string]$ExpectedPlanDigest,
  [string]$TransactionId,
  [string]$StateRoot
)

$ErrorActionPreference='Stop'
$script:ActionName=$Action.ToLowerInvariant()
$script:ConfigExplicit=[bool]$Config
# The script is a resource.  Mutable defaults belong under LOCALAPPDATA, with
# an explicit environment override for tests and portable deployments.
$script:DataRootOverride=$env:ANYTESTTOOLS_DATA_ROOT
if(-not $Config){
  $dataRoot=if($script:DataRootOverride){$script:DataRootOverride}else{Join-Path (Join-Path $env:LOCALAPPDATA 'AnyTestTools') 'FolderSwitcher'}
  $Config=Join-Path $dataRoot 'folder-config.json'
}
if(-not [IO.Path]::IsPathRooted($Config)){$Config=Join-Path $PSScriptRoot $Config}
$Config=[IO.Path]::GetFullPath($Config)
$script:ConfigDir=[IO.Path]::GetDirectoryName($Config)
if(-not $StateRoot){
  $dataRoot=if($script:DataRootOverride){$script:DataRootOverride}else{Join-Path (Join-Path $env:LOCALAPPDATA 'AnyTestTools') 'FolderSwitcher'}
  $StateRoot=Join-Path $dataRoot 'folder-data'
}
if(-not [IO.Path]::IsPathRooted($StateRoot)){$StateRoot=Join-Path $script:ConfigDir $StateRoot}
$StateRoot=[IO.Path]::GetFullPath($StateRoot)

function Out-Result([bool]$Ok,[object]$Data,[string]$Code=$null,[string]$Message=$null,[object]$Transaction=$null){
  $o=[ordered]@{schemaVersion=2;ok=$Ok;action=$script:ActionName;data=$Data;error=if($Code){[ordered]@{code=$Code;message=$Message}}else{$null}}
  if($null -ne $Transaction){$o.transaction=$Transaction}
  [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
  [Console]::WriteLine(($o|ConvertTo-Json -Depth 60 -Compress))
}
function Fail([string]$Code,[string]$Message){$detail=$Message;if($_ -and $_.Exception){$detail=$Message+' | '+$_.Exception.Message};Out-Result $false $null $Code $detail;exit 1}
function Full([string]$p,[string]$base){
  if([string]::IsNullOrWhiteSpace($p)){throw 'Path is empty.'}
  if($p -match '^(\\\\[.?]\\|\\\\2e?\\|\\\\\\?\\GLOBALROOT|/dev/|/proc/|/sys/)'){throw "Device path is not allowed: $p"}
  if([IO.Path]::IsPathRooted($p)){return [IO.Path]::GetFullPath($p)}
  return [IO.Path]::GetFullPath((Join-Path $base $p))
}
function Rel([string]$p){return $p.Replace('/','\').TrimStart('\')}
function Is-Reparse([IO.FileSystemInfo]$i){return (($i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)}
function Assert-NoReparse([string]$p,[bool]$AllowMissing=$true){
  $full=[IO.Path]::GetFullPath($p);$cur=$full
  while($true){
    if(Test-Path -LiteralPath $cur){$i=Get-Item -LiteralPath $cur -Force;if(Is-Reparse $i){throw "Reparse point is not allowed: $cur"}}
    $parent=[IO.Path]::GetDirectoryName($cur);if(!$parent -or $parent -eq $cur){break};$cur=$parent
  }
  if(-not $AllowMissing -and !(Test-Path -LiteralPath $full)){throw "Path does not exist: $full"}
  return $full
}
function Assert-Relative([string]$p,[string]$label){
  if([string]::IsNullOrWhiteSpace($p) -or [IO.Path]::IsPathRooted($p) -or $p -match '(^|[\\/])\.\.([\\/]|$)' -or $p -match '(^|[\\/])\.([\\/]|$)' -or $p -match ':'){throw "$label must be a clean relative path: $p"}
  $x=Rel $p;if($x -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)'){throw "Reserved path: $p"};return $x
}
function Hash-File([string]$p){return (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToUpperInvariant()}
function Hash-Bytes([byte[]]$b){$h=[Security.Cryptography.SHA256]::Create();try{return ([BitConverter]::ToString($h.ComputeHash($b))).Replace('-','').ToUpperInvariant()}finally{$h.Dispose()}}
function Hash-Text([string]$s){return Hash-Bytes ([Text.UTF8Encoding]::new($false).GetBytes($s))}
function Ensure-Directory([string]$p){if(!(Test-Path -LiteralPath $p -PathType Container)){New-Item -ItemType Directory -Path $p -Force|Out-Null};Assert-NoReparse $p $false|Out-Null}
function Read-Json([string]$p){return (Get-Content -LiteralPath $p -Raw -Encoding UTF8|ConvertFrom-Json)}
function Write-JsonAtomic([string]$p,[object]$v){$tmp="$p.tmp.$([guid]::NewGuid().ToString('N'))";try{$v|ConvertTo-Json -Depth 60|Set-Content -LiteralPath $tmp -Encoding UTF8;Move-Item -LiteralPath $tmp -Destination $p -Force}finally{if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}}}
function Is-Under([string]$child,[string]$parent){$c=([IO.Path]::GetFullPath($child)).TrimEnd('\');$p=([IO.Path]::GetFullPath($parent)).TrimEnd('\');return $c.Equals($p,[StringComparison]::OrdinalIgnoreCase) -or $c.StartsWith($p+'\',[StringComparison]::OrdinalIgnoreCase)}
function To-Bool([object]$v){if($v -is [bool]){return [bool]$v};if($null -eq $v){return $false};$s=([string]$v).Trim();if($s -match '^(?i:true|1|yes)$'){return $true};if($s -match '^(?i:false|0|no|)$'){return $false};throw "Invalid boolean value: $s"}
function Config-Relative([string]$p){if(Is-Under $p $script:ConfigDir){return Rel $p.Substring($script:ConfigDir.TrimEnd('\').Length+1)};return $p}

# Produce a deterministic JSON representation, independent of property insertion order.
function Canonical-Json([object]$x){
  if($null -eq $x){return 'null'}
  if($x -is [string]){return ($x|ConvertTo-Json -Compress)}
  if($x -is [bool]){return ($(if($x){'true'}else{'false'}))}
  if($x -is [int] -or $x -is [long] -or $x -is [decimal] -or $x -is [double]){return ([Convert]::ToString($x,[Globalization.CultureInfo]::InvariantCulture))}
  if($x -is [Collections.IDictionary]){$parts=@();foreach($k in @($x.Keys|Sort-Object {[string]$_})){ $parts+='"'+([string]$k).Replace('\','\\').Replace('"','\"')+'":'+(Canonical-Json $x[$k]) };return '{'+($parts -join ',')+'}' }
  if($x -is [Collections.IEnumerable] -and !($x -is [string])){$parts=@();foreach($v in $x){$parts+=(Canonical-Json $v)};return '['+($parts -join ',')+']'}
  $parts=@();foreach($p in @($x.PSObject.Properties|Sort-Object Name)){$parts+='"'+$p.Name.Replace('\','\\').Replace('"','\"')+'":'+(Canonical-Json $p.Value)};return '{'+($parts -join ',')+'}'
}
function Canonical-Hash([object]$x){return Hash-Text (Canonical-Json $x)}

function Validate-Config([object]$cfg){
  if($cfg.schemaVersion -ne 2){throw 'Config schemaVersion must be 2.'};if(!$cfg.profiles){throw 'Config has no profiles.'}
  Assert-NoReparse $script:ConfigDir $true|Out-Null;Assert-NoReparse $StateRoot $true|Out-Null
  $targets=@();$ids=@{}
  foreach($p in @($cfg.profiles)){
    if([string]::IsNullOrWhiteSpace([string]$p.id) -or $ids.ContainsKey([string]$p.id)){throw 'Profile ids must be unique and non-empty.'};$ids[[string]$p.id]=$true
    $tr=Full ([string]$p.targetRoot) $script:ConfigDir;Assert-NoReparse $tr $true|Out-Null
    if(Is-Under $tr $StateRoot -or Is-Under $StateRoot $tr){throw 'targetRoot and StateRoot must not overlap.'}
    $root=[IO.Path]::GetPathRoot($tr);if($tr.TrimEnd('\').Equals($root.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)){throw 'targetRoot cannot be a drive root.'}
    foreach($bad in @($env:SystemRoot,$env:ProgramFiles,${env:ProgramFiles(x86)},$env:windir)){if($bad -and $tr.TrimEnd('\').Equals(([IO.Path]::GetFullPath($bad)).TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)){throw 'targetRoot cannot be a system root.'}}
    foreach($old in $targets){if((Is-Under $tr $old) -or (Is-Under $old $tr)){throw 'Profile target roots overlap.'}};$targets+=$tr
    $seen=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach($m in @($p.managed)){$r=Assert-Relative ([string]$m.path) 'managed.path';if(!$seen.Add($r)){throw "Duplicate managed path: $r"};$mf=Join-Path $tr $r;if(Test-Path -LiteralPath $mf -PathType Container){throw "managed path must be a file path, not a directory: $r"};Assert-NoReparse (Split-Path -Parent $mf) $true|Out-Null}
    foreach($c in @($p.cacheDirectories)){$cr=Full ([string]$c) $script:ConfigDir;if(!(Is-Under $cr $tr)){throw "cache directory must be under targetRoot: $c"};Assert-NoReparse $cr $true|Out-Null}
    $stateIds=@{};foreach($s in @($p.states)){if([string]::IsNullOrWhiteSpace([string]$s.id) -or $stateIds.ContainsKey([string]$s.id)){throw 'State ids must be unique.'};$stateIds[[string]$s.id]=$true;if($s.snapshotRoot){$sr=Full ([string]$s.snapshotRoot) $script:ConfigDir;if(!(Is-Under $sr $StateRoot)){throw "snapshotRoot must be under StateRoot: $($s.id)"};Assert-NoReparse $sr $true|Out-Null}}
  }
  if(-not $ids.ContainsKey([string]$cfg.defaultProfileId)){throw 'defaultProfileId does not identify a profile.'}
  return $true
}
function Profile([object]$cfg){$id=if($ProfileId){$ProfileId}else{[string]$cfg.defaultProfileId};$p=@($cfg.profiles|Where-Object{$_.id -eq $id})|Select-Object -First 1;if(!$p){throw "Profile not found: $id"};return $p}
function State([object]$p){if(!$StateId){throw 'StateId is required.'};$s=@($p.states|Where-Object{$_.id -eq $StateId})|Select-Object -First 1;if(!$s){throw "State not found: $StateId"};return $s}
function Managed([object]$p){return @($p.managed|ForEach-Object{Assert-Relative ([string]$_.path) 'managed.path'})}
function Managed-Hash([string[]]$paths){return Hash-Text (($paths|ForEach-Object{Rel $_}|Sort-Object -Unique)-join "`n")}
function State-Root([object]$s){if(!$s.snapshotRoot){return $null};$r=Full ([string]$s.snapshotRoot) $script:ConfigDir;if(!(Is-Under $r $StateRoot)){throw "snapshotRoot must be under StateRoot: $r"};return $r}
function Manifest([object]$s){$r=State-Root $s;if(!$r){throw "State $($s.id) has no snapshotRoot."};$mp=Join-Path $r 'manifest.json';if(!(Test-Path -LiteralPath $mp -PathType Leaf)){throw "Manifest not found: $mp"};Assert-NoReparse $r $false|Out-Null;return [pscustomobject]@{root=$r;path=$mp;object=Read-Json $mp}}
function Manifest-Valid([object]$m,[string]$profile,[string]$state,[string]$managedHash,[string[]]$managed){
  if($m.schemaVersion -ne 2 -or [string]$m.profileId -ne $profile -or [string]$m.stateId -ne $state){throw 'Manifest identity/schema mismatch.'}
  if([string]$m.managedHash -ne $managedHash){throw 'Manifest managed list does not match config.'}
  $seen=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
  foreach($f in @($m.files)){$r=Assert-Relative ([string]$f.path) 'manifest.path';if(!$seen.Add($r)){throw "Duplicate manifest path: $r"};$exists=if($null -ne $f.exists){To-Bool $f.exists}else{$true};if($exists){if($null -eq $f.sha256 -or [string]$f.sha256 -notmatch '^[0-9A-Fa-f]{64}$'){throw "Invalid manifest hash: $r"};if([int64]$f.size -lt 0){throw 'Invalid manifest size.'}}else{if($null -ne $f.sha256 -and [string]$f.sha256 -notmatch '^[0-9A-Fa-f]{64}$'){throw "Invalid tombstone hash: $r"}}}
  $expected=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase);foreach($r in $managed){[void]$expected.Add((Rel $r))}
  if($seen.Count -ne $expected.Count){throw 'Manifest coverage does not match managed paths.'};foreach($r in $expected){if(!$seen.Contains($r)){throw "Manifest is missing managed path: $r"}}
  return $true
}
function Enumerate-Files([string]$root){Assert-NoReparse $root $false|Out-Null;$list=@();$stack=New-Object Collections.Generic.Stack[string];$stack.Push($root);while($stack.Count){$d=$stack.Pop();foreach($i in @(Get-ChildItem -LiteralPath $d -Force -ErrorAction Stop)){if(Is-Reparse $i){throw "Reparse point is not allowed: $($i.FullName)"};if($i.PSIsContainer){$stack.Push($i.FullName)}else{$list+=$i}}};return @($list|Sort-Object FullName)}
function Unmanaged-Info([string]$root,[string[]]$managed){$set=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase);foreach($r in $managed){[void]$set.Add((Rel $r))};$rows=@();foreach($i in @(Enumerate-Files $root)){$rel=Rel $i.FullName.Substring($root.TrimEnd('\').Length+1);if(!$set.Contains($rel)){$rows+=[ordered]@{path=$rel;sha256=Hash-File $i.FullName;size=$i.Length}}};$rows=@($rows|Sort-Object path);return [pscustomobject]@{count=$rows.Count;fingerprint=(Canonical-Hash $rows);files=$rows}}
function Current-Hash([string]$p){if(Test-Path -LiteralPath $p -PathType Leaf){Assert-NoReparse $p $false|Out-Null;return Hash-File $p};if(Test-Path -LiteralPath $p){throw "Target path is not a regular file: $p"};return $null}
function Get-FileMap([object]$m){$h=@{};foreach($f in @($m.files)){$exists=if($null -ne $f.exists){To-Bool $f.exists}else{$true};$h[(Rel ([string]$f.path))]=if($exists){([string]$f.sha256).ToUpperInvariant()}else{$null}};return $h}
function All-Manifests([object]$p,[string]$mh,[string[]]$managed){$a=@();foreach($s in @($p.states)){try{$x=Manifest $s;Manifest-Valid $x.object ([string]$p.id) ([string]$s.id) $mh $managed|Out-Null;$a+=[pscustomobject]@{state=$s;manifest=$x}}catch{}};return $a}
function Target-Looks-Cocos([string]$target,[object]$p){return (([IO.Path]::GetFileName($target)+' '+[string]$p.name+' '+[string]$p.id) -match '(?i)cocos|creator')}
function Guards([object]$p,[string]$target){$names=@($p.guards|ForEach-Object{[string]$_}|Where-Object{$_});if($names.Count -eq 0 -and (Target-Looks-Cocos $target $p)){$names=@('CocosCreator','Creator')};$found=@();foreach($g in $names){try{if(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($g)) -ErrorAction SilentlyContinue){$found+=$g}}catch{}};return @($found|Sort-Object -Unique)}
function Plan([object]$p,[object]$s,[object]$m,[string]$target,[string[]]$managed){
  $expected=Get-FileMap $m.object;$changes=@();$blockers=@();$all=All-Manifests $p (Managed-Hash $managed) $managed;$knownMaps=@($all|ForEach-Object{Get-FileMap $_.manifest.object})
  foreach($r in @($managed|Sort-Object)){$tp=Join-Path $target $r;if(!(Is-Under $tp $target)){throw "Managed path escapes targetRoot: $r"};$cur=Current-Hash $tp;$exp=if($expected.ContainsKey($r)){$expected[$r]}else{$null};$kind=if($null -eq $cur -and $null -eq $exp){'unchanged'}elseif($null -eq $cur){'add'}elseif($null -eq $exp){'remove'}elseif($cur -eq $exp){'unchanged'}else{'modify'};$changes+=[ordered]@{path=$r;kind=$kind;currentHash=$cur;expectedHash=$exp}
    $known=$false;foreach($map in $knownMaps){if($cur -and $map.ContainsKey($r) -and $map[$r] -eq $cur){$known=$true;break}}
    if($cur -and $cur -ne $exp){if($null -eq $exp){if(!$AllowManagedDelete){$blockers+='delete-not-authorized:'+ $r}elseif(!$known){$blockers+='delete-hash-unrecognized:'+ $r}}elseif(!$known -and !$AllowUnknown){$blockers+='target-conflict:'+ $r}}
  }
  $un=Unmanaged-Info $target $managed
  $bind=[ordered]@{profileId=[string]$p.id;stateId=[string]$s.id;targetRoot=$target;managed=(@($managed|Sort-Object));managedHash=(Managed-Hash $managed);manifestHash=(Hash-File $m.path);currentHashes=(@($changes|Sort-Object path));unmanagedFingerprint=$un.fingerprint;unmanagedFiles=$un.files;allowUnknown=(To-Bool $AllowUnknown);allowManagedDelete=(To-Bool $AllowManagedDelete)}
  $digest=Canonical-Hash $bind
  return [pscustomobject]@{changes=$changes;blockers=@($blockers|Select-Object -Unique);digest=$digest;unmanaged=$un.count;unmanagedFingerprint=$un.fingerprint}
}
function Acquire([string]$root){Ensure-Directory $root;$mutex=New-Object Threading.Mutex($false,'Local\AnyTestTools.FileSwitcher');if(!$mutex.WaitOne(0)){throw 'Another file-switcher operation is running.'};$lp=Join-Path $root 'operation.lock';try{$fs=[IO.File]::Open($lp,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}catch{$mutex.ReleaseMutex();$mutex.Dispose();throw 'Another file-switcher operation is running.'};return [pscustomobject]@{mutex=$mutex;stream=$fs;path=$lp}}
function Release($l){if($l){try{$l.stream.Dispose()}finally{Remove-Item -LiteralPath $l.path -Force -ErrorAction SilentlyContinue;$l.mutex.ReleaseMutex();$l.mutex.Dispose()}}}
function Incomplete([string]$root){$d=Join-Path $root 'transactions';if(!(Test-Path -LiteralPath $d)){return @()};$bad=@();foreach($j in @(Get-ChildItem -LiteralPath $d -Filter journal.json -Recurse -File -ErrorAction SilentlyContinue)){try{$o=Read-Json $j.FullName;if([string]$o.status -in @('started','applying','recovery-required')){$bad+=$j.FullName}}catch{$bad+=$j.FullName}};return $bad}
function Save-Journal([string]$p,[object]$j){$j|ConvertTo-Json -Depth 60|Set-Content -LiteralPath $p -Encoding UTF8}
function Restore-Touched([object]$j){$ok=$true;foreach($r in @($j.files|Where-Object{To-Bool $_.touched}|Sort-Object {[int]$_.index} -Descending)){try{$tp=Join-Path ([string]$j.targetRoot) ([string]$r.path);Assert-NoReparse (Split-Path -Parent $tp) $true|Out-Null;if(To-Bool $r.existed){if(!(Test-Path -LiteralPath $r.backup -PathType Leaf)){throw "Backup missing: $($r.path)"};if((Hash-File $r.backup) -ne [string]$r.originalHash){throw "Backup hash mismatch: $($r.path)"};Ensure-Directory (Split-Path -Parent $tp);Copy-Item -LiteralPath $r.backup -Destination $tp -Force}else{if(Test-Path -LiteralPath $tp){Remove-Item -LiteralPath $tp -Force}}}catch{$ok=$false}};return $ok}

try{
  if(!(Test-Path -LiteralPath $Config -PathType Leaf)){throw "Config not found: $Config"};$cfg=Read-Json $Config;Validate-Config $cfg|Out-Null
  if($Action -eq 'Rollback'){
    if(!$TransactionId -or $TransactionId -match '[\\/]|\.\.') {throw 'TransactionId is required and must be a simple id.'};$tx=Join-Path (Join-Path $StateRoot 'transactions') $TransactionId;$jp=Join-Path $tx 'journal.json';if(!(Test-Path -LiteralPath $jp)){throw 'Transaction journal not found.'};$j=Read-Json $jp;if([string]$j.status -eq 'rolled-back'){throw 'Transaction is already rolled back.'};$lock=Acquire $StateRoot;try{
      foreach($r in @($j.files|Where-Object{To-Bool $_.touched})){$tp=Join-Path ([string]$j.targetRoot) ([string]$r.path);$now=Current-Hash $tp;if(To-Bool $r.newExists){if($now -ne [string]$r.newHash){throw "Rollback guard failed: $($r.path)"}}else{if($null -ne $now){throw "Rollback guard failed: $($r.path)"};};if(To-Bool $r.existed){if(!(Test-Path -LiteralPath $r.backup -PathType Leaf)){throw "Backup missing: $($r.path)"};if((Hash-File $r.backup) -ne [string]$r.originalHash){throw "Backup hash mismatch: $($r.path)"}}}
      foreach($r in @($j.files|Where-Object{To-Bool $_.touched}|Sort-Object {[int]$_.index} -Descending)){$tp=Join-Path ([string]$j.targetRoot) ([string]$r.path);if(To-Bool $r.existed){Ensure-Directory (Split-Path -Parent $tp);Copy-Item -LiteralPath $r.backup -Destination $tp -Force}else{if(Test-Path -LiteralPath $tp){Remove-Item -LiteralPath $tp -Force}}};$j.status='rolled-back';Save-Journal $jp $j;Out-Result $true ([ordered]@{transactionId=$TransactionId;status='rolled-back';restoredCount=@($j.files|Where-Object{To-Bool $_.touched}).Count}) $null $null ([ordered]@{id=$TransactionId;status='rolled-back'})
    }finally{Release $lock};exit 0
  }
  $p=Profile $cfg;$target=Full ([string]$p.targetRoot) $script:ConfigDir;Assert-NoReparse $target $false|Out-Null;$managed=Managed $p;$mh=Managed-Hash $managed
  if($Action -eq 'Scan'){$root=if($SourceRoot){Full $SourceRoot $script:ConfigDir}else{$target};Assert-NoReparse $root $false|Out-Null;$rows=@(Enumerate-Files $root|ForEach-Object{[ordered]@{path=Rel $_.FullName.Substring($root.TrimEnd('\').Length+1);size=$_.Length;sha256=Hash-File $_.FullName}});Out-Result $true ([ordered]@{profileId=$p.id;targetRoot=$target;scanRoot=$root;files=$rows;managedCount=$managed.Count;unmanagedCount=if($root -eq $target){(Unmanaged-Info $target $managed).count}else{$null}});exit 0}
  if($Action -eq 'Status'){$entries=@();$exact=@();$errs=@();foreach($s in @($p.states)){try{$m=Manifest $s;Manifest-Valid $m.object ([string]$p.id) ([string]$s.id) $mh $managed|Out-Null;$map=Get-FileMap $m.object;$ok=$true;foreach($r in $managed){$tp=Join-Path $target $r;$cur=Current-Hash $tp;$exp=$map[$r];if($cur -ne $exp){$ok=$false;break}};if($ok){$exact+=[string]$s.id};$entries+=[ordered]@{id=$s.id;name=$s.name;exact=$ok;fileCount=@($m.object.files).Count;manifest=$m.path}}catch{$errs+=[ordered]@{stateId=$s.id;message=$_.Exception.Message};$entries+=[ordered]@{id=$s.id;name=$s.name;exact=$false;error=$_.Exception.Message}}};$current='unknown';if($exact.Count -eq 1){$current=$exact[0]}elseif($exact.Count -gt 1){$current='ambiguous'};Out-Result $true ([ordered]@{profileId=$p.id;profileName=$p.name;currentMode=$current;targetRoot=$target;managedCount=$managed.Count;unmanagedCount=(Unmanaged-Info $target $managed).count;states=$entries;creatorRunning=((Guards $p $target).Count -gt 0);blockedProcesses=@(Guards $p $target);snapshotErrors=$errs;cacheDirectories=@($p.cacheDirectories)});exit 0}
  $s=State $p
  if($Action -eq 'Capture'){
    $src=if($SourceRoot){Full $SourceRoot $script:ConfigDir}else{$target};Assert-NoReparse $src $false|Out-Null;if(Is-Under $src $StateRoot){throw 'SourceRoot must not be inside StateRoot.'};$lock=Acquire $StateRoot;$stage=$null;try{
      $stage=Join-Path (Join-Path $StateRoot 'staging') ('capture-'+[guid]::NewGuid().ToString('N'));Ensure-Directory $stage;$rows=@();$missing=0;foreach($r in $managed){$sp=Join-Path $src $r;if(Test-Path -LiteralPath $sp -PathType Container){throw "Source managed path is a directory: $r"};if(!(Test-Path -LiteralPath $sp -PathType Leaf)){$missing++;continue};Assert-NoReparse $sp $false|Out-Null;$dp=Join-Path $stage $r;Ensure-Directory (Split-Path -Parent $dp);Copy-Item -LiteralPath $sp -Destination $dp;$rows+=[ordered]@{path=$r;exists=$true;size=(Get-Item $dp).Length;sha256=Hash-File $dp}};foreach($r in $managed){if(!(@($rows|Where-Object{$_.path -eq $r}).Count)){$rows+=[ordered]@{path=$r;exists=$false;size=0;sha256=$null}}};$unique=[guid]::NewGuid().ToString('N');$store=Join-Path (Join-Path (Join-Path $StateRoot 'snapshots') ([string]$p.id)) ([string]$s.id);$final=Join-Path $store $unique;$manifest=[ordered]@{schemaVersion=2;profileId=$p.id;stateId=$s.id;createdUtc=[DateTime]::UtcNow.ToString('o');managedHash=$mh;managedRevision=$mh;managedPaths=@($managed|Sort-Object);files=@($rows|Sort-Object path)};Set-Content -LiteralPath (Join-Path $stage 'manifest.json') -Value ($manifest|ConvertTo-Json -Depth 60) -Encoding UTF8;foreach($f in @($rows)){if((To-Bool $f.exists) -and (Hash-File (Join-Path $stage $f.path)) -ne $f.sha256){throw "Staged hash mismatch: $($f.path)"}};if(!$DryRun){Ensure-Directory $store;Move-Item -LiteralPath $stage -Destination $final -Force;$stage=$null;$cfg2=Read-Json $Config;foreach($pp in @($cfg2.profiles)){if($pp.id -eq $p.id){foreach($ss in @($pp.states)){if($ss.id -eq $s.id){$ss.snapshotRoot=Config-Relative $final}}}};$backup="$Config.bak.$unique";Copy-Item -LiteralPath $Config -Destination $backup -Force;Write-JsonAtomic $Config $cfg2};Out-Result $true ([ordered]@{profileId=$p.id;stateId=$s.id;sourceRoot=$src;fileCount=$rows.Count;missingCount=$missing;dryRun=[bool]$DryRun;snapshotRoot=if($DryRun){$null}else{$final}});exit 0
    }finally{if($stage -and (Test-Path -LiteralPath $stage)){Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue};Release $lock}
  }
  $m=Manifest $s;Manifest-Valid $m.object ([string]$p.id) ([string]$s.id) $mh $managed|Out-Null;$pl=Plan $p $s $m $target $managed;$data=[ordered]@{profileId=$p.id;stateId=$s.id;targetRoot=$target;changes=$pl.changes;counts=[ordered]@{add=@($pl.changes|Where-Object{$_.kind -eq 'add'}).Count;modify=@($pl.changes|Where-Object{$_.kind -eq 'modify'}).Count;remove=@($pl.changes|Where-Object{$_.kind -eq 'remove'}).Count;unchanged=@($pl.changes|Where-Object{$_.kind -eq 'unchanged'}).Count};unmanagedCount=$pl.unmanaged;unmanagedFingerprint=$pl.unmanagedFingerprint;blockers=$pl.blockers;planDigest=$pl.digest}
  if($Action -eq 'Diff'){Out-Result $true $data;exit 0};if($DryRun){$data.dryRun=$true;Out-Result $true $data;exit 0};if([string]::IsNullOrWhiteSpace($ExpectedPlanDigest)){throw 'ExpectedPlanDigest is required and must match the current plan.'}
  if((Guards $p $target).Count){throw 'Guard process is running.'};$lock=Acquire $StateRoot;$j=$null;$jp=$null;try{
    if((Guards $p $target).Count){throw 'Guard process is running.'};if((Incomplete $StateRoot).Count){throw 'Incomplete or recovery-required transaction exists.'};$m=Manifest $s;Manifest-Valid $m.object ([string]$p.id) ([string]$s.id) $mh $managed|Out-Null;$pl=Plan $p $s $m $target $managed;if($ExpectedPlanDigest -ne $pl.digest){throw 'ExpectedPlanDigest is stale; recompute Diff before Apply.'};if($pl.blockers.Count){throw ('Apply blocked: '+($pl.blockers -join ', '))}
    $tid=([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8));$tx=Join-Path (Join-Path $StateRoot 'transactions') $tid;Ensure-Directory (Join-Path $tx 'backups');Ensure-Directory (Join-Path $tx 'stage');$j=[ordered]@{schemaVersion=2;profileId=$p.id;stateId=$s.id;targetRoot=$target;status='started';files=@()};$jp=Join-Path $tx 'journal.json';Save-Journal $jp $j;$idx=0;foreach($c in @($pl.changes|Where-Object{$_.kind -ne 'unchanged'})){$tp=Join-Path $target $c.path;$rec=[ordered]@{index=$idx;path=$c.path;existed=($null -ne $c.currentHash);originalHash=$c.currentHash;backup=$null;newExists=($null -ne $c.expectedHash);newHash=$c.expectedHash;touched=$false};if($rec.existed){$rec.backup=Join-Path $tx ('backups\'+('{0:D6}.bak'-f $idx));Copy-Item -LiteralPath $tp -Destination $rec.backup -Force};$j.files+=$rec;$idx++};$j.status='applying';Save-Journal $jp $j;$idx=0;foreach($rec in @($j.files)){$tp=Join-Path $target $rec.path;$rec.touched=$true;Save-Journal $jp $j;if(To-Bool $rec.newExists){$sp=Join-Path (State-Root $s) $rec.path;$st=Join-Path $tx ('stage\'+('{0:D6}.tmp'-f $idx));Assert-NoReparse $sp $false|Out-Null;Ensure-Directory (Split-Path -Parent $st);Copy-Item -LiteralPath $sp -Destination $st -Force;if((Hash-File $st) -ne $rec.newHash){throw "Snapshot hash mismatch: $($rec.path)"};Ensure-Directory (Split-Path -Parent $tp);Move-Item -LiteralPath $st -Destination $tp -Force}else{if(Test-Path -LiteralPath $tp){Remove-Item -LiteralPath $tp -Force}};Save-Journal $jp $j;$idx++};$j.status='completed';Save-Journal $jp $j;Out-Result $true ([ordered]@{profileId=$p.id;stateId=$s.id;changedCount=@($pl.changes|Where-Object{$_.kind -ne 'unchanged'}).Count;unmanagedUntouched=$true;planDigest=$pl.digest}) $null $null ([ordered]@{id=$tid;status='completed';path=$tx})
  }catch{if($j){$restored=Restore-Touched $j;if($restored){$j.status='failed-rolled-back'}else{$j.status='recovery-required'};Save-Journal $jp $j};throw}finally{Release $lock}
}catch{Fail 'operation-failed' $_.Exception.Message}
