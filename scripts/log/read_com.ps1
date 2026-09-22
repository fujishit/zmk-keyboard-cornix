<#
.SYNOPSIS
  Stream a Windows serial port (USB CDC-ACM) to stdout, for scripts/log/capture.py.

.DESCRIPTION
  WSL2 does not see USB devices unless they are attached with usbipd-win, and
  /dev/ttyS* are NOT the Windows USB CDC ports. This script is the way out:
  capture.py runs it through powershell.exe (the Windows interop binary that is
  always on PATH inside WSL) and reads its stdout, so the keyboard's debug log
  reaches WSL without usbipd and without any Python serial package.

  Raw device text is written to stdout unchanged (no newline rewriting, no
  PowerShell object formatting); every status message goes to stderr.

.PARAMETER Port
  Windows port name, e.g. COM5.

.PARAMETER Baud
  Baud rate; a CDC-ACM port ignores it, 115200 matches capture.py's default.

.NOTES
  Keep this file pure ASCII: Windows PowerShell 5.1 reads a .ps1 without a BOM
  in the machine's ANSI code page, so a non-ASCII literal here would be mangled
  on a localized Windows (tests/log/test_capture.py checks this).

  Exit codes (capture.py depends on them):
    3  the port could not be opened (does not exist / in use / access denied)
    4  the port was open and then failed (device unplugged, keyboard reset):
       capture.py waits and re-opens, exactly like the termios path on EIO.
#>
param(
    [Parameter(Mandatory = $true)][string]$Port,
    [int]$Baud = 115200
)

$ErrorActionPreference = 'Stop'

# Device text is UTF-8; without this the console re-encodes it to the OEM code
# page and non-ASCII bytes reach capture.py mangled.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$out = [Console]::Out
$err = [Console]::Error

try {
    $sp = [System.IO.Ports.SerialPort]::new($Port, $Baud, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $sp.Handshake = [System.IO.Ports.Handshake]::None
    # A CDC-ACM console only transmits once the host asserted DTR.
    $sp.DtrEnable = $true
    $sp.RtsEnable = $true
    $sp.ReadTimeout = 250
    $sp.WriteTimeout = 500
    $sp.NewLine = "`n"
    $sp.Encoding = [System.Text.Encoding]::UTF8
    $sp.Open()
} catch {
    $err.WriteLine("read_com: cannot open ${Port}: $($_.Exception.Message)")
    exit 3
}

$err.WriteLine("read_com: opened $Port at $Baud")
$err.Flush()

try {
    while ($true) {
        # ReadExisting() returns whatever arrived since the last call (possibly
        # a partial line) and never blocks; capture.py reassembles the lines and
        # stamps each one with the host clock, so partial reads are harmless.
        $chunk = $sp.ReadExisting()
        if ($chunk.Length -gt 0) {
            $out.Write($chunk)
            $out.Flush()
        } else {
            if (-not $sp.IsOpen) { throw "port closed" }
            Start-Sleep -Milliseconds 20
        }
    }
} catch {
    $err.WriteLine("read_com: $Port lost: $($_.Exception.Message)")
    try { $sp.Close() } catch { }
    exit 4
}
