#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import os
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QUICK_BACKEND']='software'
import argparse,sys,tempfile,time
from pathlib import Path
from PySide6.QtCore import QCoreApplication,QObject,QUrl,QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.wizard import WizardController
args=argparse.ArgumentParser();args.add_argument('--screenshots',type=Path);args=args.parse_args()
QQuickStyle.setStyle('Basic');app=QGuiApplication([])
def wait(test):
    end=time.monotonic()+30
    while not test():
        if time.monotonic()>end:raise AssertionError(c.data['error'] or 'GUI timeout')
        QCoreApplication.processEvents();QTest.qWait(10)
with tempfile.TemporaryDirectory() as d:
    c=WizardController(True,None,Path(d));qml=QQmlApplicationEngine();qml.rootContext().setContextProperty('backend',c);qml.load(QUrl.fromLocalFile(str(PROJECT/'installer/liuqin_gui/qml/Main.qml')));assert qml.rootObjects();w=qml.rootObjects()[0]
    wait(lambda:c.data['workerReady'] and c.data['environment'] and not c.data['busy'])
    assert w.findChild(QObject,'romPreviewEntry').property('enabled')
    for pid,policy,expected in [('miui14','preserve','requires-avb-mode'),('hyperos206','preserve','requires-avb-mode'),('hyperos206','rom','offline-ready'),('miku26101322','preserve','offline-ready'),('lineage241022','preserve','unknown-metadata')]:
        c.startRomPreview();c.setRomPreviewOption('source','reference');c.setRomPreviewOption('reference',pid);c.setRomPreviewOption('firmware',policy)
        assert not c.data['romPreview'];c.prepareWorkflow();wait(lambda:not c.data['busy'] and bool(c.data['romPreview']))
        r=c.data['romPreview'];assert r['rec_after']['status']==expected;assert not r['executable'];assert c.data['wizardStep']==2
        assert not w.findChild(QObject,'executeButton').property('visible');assert not w.findChild(QObject,'operationConsent').property('visible')
        assert 'AVB flags=' in w.findChild(QObject,'romPreviewAfter').property('text')
        before=c.data['state'];c.executeWorkflow(c.data['selectedSerial'],True,True);assert c.data['state']==before and not c.data['busy']
        if args.screenshots:
            args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(100);assert w.grabWindow().save(str(args.screenshots/(pid+'-'+policy+'.png')))
        c.back(1);c.setMaterial('android','changed.zip');assert not c.data['romPreview']
    c.back(0);c.selectDevice(c.data['selectedSerial']);assert not c.data['romPreview'] and not c.data['romPreviewMode']
    wait(lambda:not c.data['busy']);c.process.closeWriteChannel();c.process.waitForFinished(5000);qml.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
print('PASS: five ROM/policy previews, readonly workflow, no execution and stale-preview invalidation')
