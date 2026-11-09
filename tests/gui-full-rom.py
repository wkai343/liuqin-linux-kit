#!/usr/bin/env python3
import os
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QUICK_BACKEND']='software'
import sys,tempfile,time
from pathlib import Path
from PySide6.QtCore import QCoreApplication,QMetaObject,QObject,QUrl,QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'installer'))
from liuqin_gui.wizard import WizardController
QQuickStyle.setStyle('Basic');app=QGuiApplication([])
def wait(fn):
 end=time.monotonic()+45
 while not fn():
  if time.monotonic()>end:raise AssertionError(c.data['error'] or 'timeout')
  QCoreApplication.processEvents();QTest.qWait(10)
with tempfile.TemporaryDirectory() as d:
 c=WizardController(True,None,Path(d));qml=QQmlApplicationEngine();qml.rootContext().setContextProperty('backend',c);qml.load(QUrl.fromLocalFile(str(ROOT/'installer/liuqin_gui/qml/Main.qml')));assert qml.rootObjects();w=qml.rootObjects()[0]
 wait(lambda:c.data['workerReady'] and not c.data['busy'] and c.data['compatibility'])
 read_button=w.findChild(QObject,'readDeviceButton')
 assert read_button.property('enabled')
 assert QMetaObject.invokeMethod(read_button,'clicked')
 wait(lambda:c.data['inventorySummary'] and not c.data['busy'])
 assert c.data['wizardStep']==0 and c.data['installTargets']==[]
 c.setInstallTarget('userdata',True)
 next_button=w.findChild(QObject,'partitionNext')
 assert next_button.property('enabled')
 assert QMetaObject.invokeMethod(next_button,'clicked')
 assert c.data['wizardStep']==1 and c.data['installTargets']==['userdata']
 assert c.data['targetKind']=='android' and not c.data['romPreviewMode']
 choice=w.findChild(QObject,'fixedRecoveryPolicyChoice');assert choice and choice.property('visible')
 assert c.data['fixedRecoveryPolicy']=='auto' and choice.property('currentIndex')==0
 c.setFixedRecoveryPolicy('rom');QTest.qWait(20);assert choice.property('currentIndex')==1
 c.setFixedRecoveryPolicy('auto');QTest.qWait(20);assert choice.property('currentIndex')==0
 c.setMaterial('android','demo-rom');c.prepareWorkflow();wait(lambda:c.data['state']=='prepared' and not c.data['busy'])
 assert c.data['wizardPlan']['fixed_recovery_policy']=='auto'
 button=w.findChild(QObject,'executeButton');assert not button.property('enabled')
 serial=w.findChild(QObject,'confirmationSerial');assert serial.property('readOnly') and serial.property('text')==c.data['selectedSerial']
 w.findChild(QObject,'operationConsent').setProperty('checked',True);QTest.qWait(50);assert button.property('enabled')
 c.executeWorkflow(c.data['selectedSerial'],True,True);wait(lambda:c.data['state']=='wizard_complete' and not c.data['busy']);assert c.data['receipt']['system_installed']
 c.scanDevice();wait(lambda:not c.data['busy']);assert c.data['inventorySummary'] and c.data['partitionRows']
 c.process.closeWriteChannel();c.process.waitForFinished(5000);qml.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
print('PASS: read partitions, select only userdata, confirm and install Android; demo-only')
