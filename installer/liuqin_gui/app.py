# SPDX-License-Identifier: MIT
from pathlib import Path
import signal
import sys

from PySide6.QtCore import QProcess, QStandardPaths, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .controller import Controller
from liuqin_identity import PROJECT_NAME


def install_interrupt_handler(app, controller):
    """Defer terminal Ctrl+C to the window's normal, write-aware close path."""
    requested = False

    def request_interrupt(*_args):
        nonlocal requested
        requested = True

    previous = signal.signal(signal.SIGINT, request_interrupt)
    timer = QTimer(app)

    def check_interrupt():
        nonlocal requested
        if requested:
            requested = False
            if controller.closeWindow():
                app.quit()

    timer.timeout.connect(check_interrupt)
    timer.start(100)

    def restore():
        timer.stop()
        signal.signal(signal.SIGINT, previous)

    return restore


def main(args):
    QQuickStyle.setStyle('Basic')
    app = QGuiApplication([sys.argv[0]])
    app.setOrganizationName('Liuqin')
    app.setApplicationName('LinuxInstaller')
    app.setApplicationDisplayName(PROJECT_NAME)
    state_dir = args.state_dir or Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    bundle = args.bundle
    controller = Controller(args.demo, bundle, state_dir)
    controller.quitRequested.connect(app.quit)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('backend', controller)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / 'qml/Main.qml')))
    if not engine.rootObjects():
        controller.closeWindow()
        raise SystemExit('QML 界面加载失败。')
    if args.screenshot:
        if not args.demo:
            raise SystemExit('截图预览仅允许 --demo，避免打开真机流程。')

        def screenshot():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            image = engine.rootObjects()[0].grabWindow()
            if image.isNull() or not image.save(str(args.screenshot)):
                raise RuntimeError('无法保存界面截图。')
            controller.closeWindow()
            # Wait asynchronously for the worker to finish, never block GUI.
            controller.process.finished.connect(app.quit)
            if controller.process.state() == QProcess.ProcessState.NotRunning:
                app.quit()
        QTimer.singleShot(1200, screenshot)
    restore_interrupt = install_interrupt_handler(app, controller)
    try:
        result = app.exec()
        # A normally closed window is already idle and has sent shutdown.
        if controller.process.state() != QProcess.ProcessState.NotRunning:
            controller.process.finished.connect(lambda *_: None)
            controller.process.waitForFinished(3000)
    finally:
        restore_interrupt()
    raise SystemExit(result)
