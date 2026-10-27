"""A munkaszál nem érhet el Qt vezérlőket; az eredmény a GUI szálára érkezik."""
from PySide6.QtCore import QObject, QThread, Slot
from shiboken6 import isValid

_jobs = set()


class _Task(QThread):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.result = None
        self.error = None

    @Slot()
    def run(self):
        try:
            self.result = self.function()
        except Exception as error:
            self.error = error
        finally:
            self.function = None


class _Delivery(QObject):
    def __init__(self, parent, task, success, failure):
        super().__init__()
        self.owner = parent
        self.task = task
        self.success = success
        self.failure = failure

    @Slot()
    def finished(self):
        try:
            if isValid(self.owner):
                if self.task.error is None:
                    self.success(self.task.result)
                else:
                    self.failure(self.task.error)
        finally:
            _jobs.discard(self)
            self.task.deleteLater()
            self.deleteLater()


def run_task(parent, function, on_success, on_error):
    task = _Task(function)
    delivery = _Delivery(parent, task, on_success, on_error)
    _jobs.add(delivery)
    task.finished.connect(delivery.finished)
    task.start()
    return delivery


def wait_for_tasks():
    for delivery in tuple(_jobs):
        delivery.task.wait()
