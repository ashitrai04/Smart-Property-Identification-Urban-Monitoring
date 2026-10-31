"""Run an area job without freezing QGIS.

The panel cannot simply poll in the GUI thread: an area job is minutes, and
every blocking request would stutter the canvas. QgsTask puts the polling on a
worker thread; results come back as signals, which Qt delivers on the GUI
thread, so the layer is only ever touched from where it is safe to touch it.

The task emits buildings as they arrive rather than at the end, because the
server publishes each window as it finishes and watching the work is the point.
"""
from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .. import client


class AreaJobTask(QgsTask):
    # list of {"reg": ring, "raw": ring, "area_m2": float}
    found = pyqtSignal(list)
    note = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, server, task, aoi, params, description="Delineating buildings"):
        super().__init__(description, QgsTask.CanCancel)
        self.server, self.task, self.aoi, self.params = server, task, aoi, params
        self.job_id = None
        self.total = 0
        self._error = ""

    def run(self):
        """Worker thread: start the job, then poll until it ends."""
        try:
            job = client.start_area(self.aoi, task=self.task, server=self.server, **self.params)
        except client.ServerError as e:
            self._error = str(e)
            return False
        self.job_id = job["id"]
        self.note.emit(f"Job {self.job_id} queued on {job['imagery']['name']}")

        since = 0
        while True:
            if self.isCanceled():
                self._cancel_remote()
                return False
            try:
                live = client.area_live(self.job_id, since=since, server=self.server)
            except client.ServerError as e:
                # One failed poll is not a failed job - a tunnel hiccups.
                self.note.emit(f"(retrying: {e})")
                self._sleep(2)
                continue

            since = live.get("next", since)
            items = live.get("items") or []
            if items:
                self.total += len(items)
                self.found.emit(items)
            self.setProgress(float(live.get("percent") or 0))

            status = live.get("status")
            if status == "done":
                return True
            if status in ("failed", "cancelled"):
                try:
                    st = client.area_status(self.job_id, server=self.server)
                    self._error = st.get("message") or f"job {status}"
                except client.ServerError:
                    self._error = f"job {status}"
                return False
            self._sleep(1.2)

    def _sleep(self, seconds):
        """Sleep in short slices so cancelling is responsive."""
        import time
        end = time.time() + seconds
        while time.time() < end:
            if self.isCanceled():
                return
            time.sleep(0.1)

    def _cancel_remote(self):
        if not self.job_id:
            return
        try:
            client.area_cancel(self.job_id, server=self.server)
        except client.ServerError:
            pass        # the job ends on its own soon enough

    def finished(self, ok):
        if not ok and self._error:
            self.failed.emit(self._error)
