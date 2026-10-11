"""Quick test for persistent PS host behavior with open_folder script."""
import asyncio
import sys
sys.path.insert(0, ".")
from winbrow.pshost import run_persistent

async def test():
    # Simple test
    r = await run_persistent('Write-Output "hello from pshost"')
    print("Simple test:", r)
    print()
    
    # open_folder-like script (the actual template rendered with folder=downloads)
    script = (
        '$requested = "downloads".Trim().Trim(\'"\').Trim("\'")' "\n"
        "$folderMap = @{\n"
        '    "downloads"  = Join-Path $env:USERPROFILE "Downloads"\n'
        '    "documents"  = [Environment]::GetFolderPath(\'MyDocuments\')\n'
        "}\n"
        "$key = $requested.ToLower()\n"
        "if ($folderMap.ContainsKey($key)) {\n"
        "    $target = $folderMap[$key]\n"
        '    Start-Process explorer.exe -ArgumentList "$target"\n'
        '    return "Opened $key folder"\n'
        "}\n"
    )
    print("Script:\n", script)
    print()
    r2 = await run_persistent(script)
    print("Folder test:", r2)

asyncio.run(test())
