"""Windows Service wrapper for OmniRouter.

Install / control on a Windows host (run an elevated PowerShell):

    pip install pywin32
    python -m omnirouter.service_windows install
    python -m omnirouter.service_windows start
    python -m omnirouter.service_windows stop
    python -m omnirouter.service_windows remove

The service simply boots the same uvicorn + DICOM SCP that ``main.py``
boots when run interactively, so the UI is reachable at
``http://localhost:<WEB_PORT>/`` (default 5000) on the local machine.

Alternative (recommended for many sites) — use NSSM:

    nssm install OmniRouter "C:\\Path\\To\\python.exe" "-m" "omnirouter.main"
    nssm set OmniRouter AppDirectory "C:\\Path\\To\\OmniRouter"
    nssm start OmniRouter
"""

from __future__ import annotations

import logging
import sys
import threading

try:
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore
except ImportError:  # pragma: no cover — module only usable on Windows
    print(
        "pywin32 is required to host OmniRouter as a Windows service.\n"
        "Install it on the target machine with:  pip install pywin32",
        file=sys.stderr,
    )
    sys.exit(1)

import uvicorn

from .config import WEB_HOST, WEB_PORT
from .log_bus import configure_logging


class OmniRouterService(win32serviceutil.ServiceFramework):
    _svc_name_ = "OmniRouter"
    _svc_display_name_ = "OmniRouter DICOM Router"
    _svc_description_ = (
        "Listens for DICOM C-STORE requests on port 7776 (AE OMNI) and "
        "forwards received studies to a remote PACS over DICOM TLS."
    )

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self._server is not None:
            self._server.should_exit = True
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        configure_logging(level=logging.INFO)
        config = uvicorn.Config(
            "omnirouter.web:app",
            host=WEB_HOST,
            port=WEB_PORT,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(OmniRouterService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(OmniRouterService)
