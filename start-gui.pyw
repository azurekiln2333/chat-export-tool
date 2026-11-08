"""Source-checkout launcher; shares backend packages with the local environment."""
from pathlib import Path
import os
import site
import sys

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
local_packages = root / ".venv" / "Lib" / "site-packages"
if local_packages.is_dir():
    # Append rather than prepend: the launching Python owns its Qt binaries.
    site.addsitedir(str(local_packages))

try:
    runtime = Path(os.environ.get("CHAT_ARCHIVE_RUNTIME") or root / ".work" / "gui").expanduser().resolve()
    temporary = runtime / "temp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ["CHAT_ARCHIVE_RUNTIME"] = str(runtime)
    os.environ["TEMP"] = os.environ["TMP"] = str(temporary)
    from duoshan_export.gui import main
    raise SystemExit(main())
except Exception as error:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(error), "Chat Archive 启动失败", 0x10)
    else:
        raise
