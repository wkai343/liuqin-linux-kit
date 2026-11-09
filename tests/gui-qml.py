#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Current QML, real demo worker, signal handling and guarded shutdown."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_QUICK_BACKEND']='software'
from PySide6.QtCore import QCoreApplication,QEvent,QMetaObject,QObject,QProcess,QTimer,QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.wizard import WizardController
from liuqin_gui.app import install_interrupt_handler

parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
QQuickStyle.setStyle('Basic');app=QGuiApplication([sys.argv[0]])


def wait(predicate,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QCoreApplication.processEvents()
        if predicate():return
        QTest.qWait(10)
    raise AssertionError('GUI/worker timeout: '+controller.data['error'])


def capture(name):
    if args.screenshots:
        args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(100)
        assert window.grabWindow().save(str(args.screenshots/name))


with tempfile.TemporaryDirectory() as directory:
    controller=WizardController(True,None,Path(directory))
    timers=set(app.findChildren(QTimer))
    restore_interrupt=install_interrupt_handler(app,controller)
    interrupt_timer=next(timer for timer in app.findChildren(QTimer) if timer not in timers)
    qml=QQmlApplicationEngine();qml.rootContext().setContextProperty('backend',controller)
    qml.load(QUrl.fromLocalFile(str(PROJECT/'installer/liuqin_gui/qml/Main.qml')))
    assert qml.rootObjects(),'Current product QML did not load'
    window=qml.rootObjects()[0]
    try:
        wait(lambda:controller.data['workerReady'] and controller.data['compatibility'] and not controller.data['busy'])
        assert controller.data['devices'][0]['serial']=='DEMO-LIUQIN'
        assert not controller.data['inventorySummary']
        capture('connection.png')
        controller.scanDevice();wait(lambda:controller.data['inventorySummary'] and not controller.data['busy'])
        capture('partitions.png')
        controller.setInstallTarget('userdata',True);controller.nextFromPartitions()
        assert controller.data['wizardStep']==1
        controller.setMaterial('android','demo-rom');capture('materials.png')
        controller.prepareWorkflow();wait(lambda:controller.data['state']=='prepared' and not controller.data['busy'])
        assert controller.data['wizardPlan']['kind']=='android'
        original_backup=controller.data['backupPath']
        controller.setBackupPath('/unconfirmed-backup-change')
        assert controller.data['backupPath']==original_backup,'Prepared plan must freeze backup configuration'
        execute=window.findChild(QObject,'executeButton')
        assert execute is not None and not execute.property('enabled')
        window.findChild(QObject,'operationConsent').setProperty('checked',True)
        assert execute.property('enabled');capture('confirmation.png')
        assert QMetaObject.invokeMethod(execute,'clicked')
        wait(lambda:controller.data['state']=='installing')
        assert not controller.closeWindow(),'An active installation must keep the window open'
        controller.update(error='')
        signal.raise_signal(signal.SIGINT)
        # Deliver the timer callback before pumping more worker events: this
        # deterministically tests Ctrl+C during a real in-flight demo job.
        interrupt_timer.timeout.emit()
        assert controller.process.state()==QProcess.ProcessState.Running
        assert '当前操作仍在进行' in controller.data['error']
        wait(lambda:controller.data['state']=='wizard_complete' and not controller.data['busy'])
        assert controller.data['receipt']['completed'] and controller.data['receipt']['system_installed']
        assert controller.data['wizardStep']==4 and not controller.data['error']
        capture('result.png')
        controller.exportReport(directory)
        report_path=next(Path(directory).glob('liuqin-report-*.json'))
        content=report_path.read_text();report=json.loads(content)
        assert report['simulation'] and report['state']=='wizard_complete'
        assert 'DEMO-LIUQIN' not in content and '<device-serial>' in content
        assert str(Path.home()) not in content
        # Idle Ctrl+C must use the same normal shutdown path.
        signal.raise_signal(signal.SIGINT);interrupt_timer.timeout.emit()
        wait(lambda:controller.process.state()==QProcess.ProcessState.NotRunning)
    finally:
        restore_interrupt()
        if controller.process.state()!=QProcess.ProcessState.NotRunning:
            controller.process.closeWriteChannel();controller.process.waitForFinished(10000)
        qml.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

# A separately prepared plan must cancel cleanly before its worker can exit.
with tempfile.TemporaryDirectory() as directory:
    controller=WizardController(True,None,Path(directory))
    wait(lambda:controller.data['compatibility'] and not controller.data['busy'])
    controller.scanDevice();wait(lambda:controller.data['inventorySummary'] and not controller.data['busy'])
    controller.setInstallTarget('userdata',True);controller.nextFromPartitions()
    controller.setMaterial('android','demo-rom');controller.prepareWorkflow()
    wait(lambda:controller.data['state']=='prepared' and not controller.data['busy'])
    assert not controller.closeWindow()
    wait(lambda:controller.process.state()==QProcess.ProcessState.NotRunning)
    assert controller.data['state']=='cancelled'
print('PASS: current QML, consent, frozen plan, guarded Ctrl+C, report export and prepared shutdown')
