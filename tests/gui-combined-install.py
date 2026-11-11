#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One partition-first plan for optional Android and multiple Linux systems. No device I/O."""
import argparse,os,sys,tempfile,time
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QUICK_BACKEND']='software'
from PySide6.QtCore import QCoreApplication,QObject,QUrl,QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'installer'))
from liuqin_gui.wizard import WizardController
p=argparse.ArgumentParser();p.add_argument('--screenshots',type=Path);args=p.parse_args()
QQuickStyle.setStyle('Basic');app=QGuiApplication([])
def wait(fn):
 end=time.monotonic()+90
 while not fn():
  if time.monotonic()>end:raise AssertionError(c.data['error'] or 'timeout')
  QCoreApplication.processEvents();QTest.qWait(10)
def capture(name):
 if args.screenshots:
  args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(100);w.grabWindow().save(str(args.screenshots/(name+'.png')))
with tempfile.TemporaryDirectory() as d:
 c=WizardController(True,None,Path(d));qml=QQmlApplicationEngine();qml.rootContext().setContextProperty('backend',c);qml.load(QUrl.fromLocalFile(str(ROOT/'installer/liuqin_gui/qml/Main.qml')));assert qml.rootObjects();w=qml.rootObjects()[0]
 wait(lambda:c.data['workerReady'] and not c.data['busy'] and c.data['compatibility']);c.scanDevice();wait(lambda:c.data['inventorySummary'] and not c.data['busy'])
 c.editPartition('userdata','delete','');c.createPartition('android',32,'f2fs','Android')
 c.createPartition('linux',32,'ext4','Fedora');first=c.data['selectedPartition']
 c.createPartition('linux',32,'btrfs','Linux Btrfs');second=c.data['selectedPartition']
 c.setInstallTarget('userdata',True);c.setInstallTarget(first,True);assert len(c.data['installTargets'])==3
 c.setInstallTarget(first,False);assert len(c.data['installTargets'])==2;c.setInstallTarget(first,True)
 c.nextFromPartitions();assert c.data['targetKind']=='combined';c.setDefaultTarget(second)
 c.setMaterial('android','demo-rom');c.setMaterial('linux',str(Path(d)/'demo-materials'))
 for name in (first,second):c.setLinuxMaterial(name,str(Path(d)/'demo-materials'))
 QCoreApplication.processEvents()
 assert w.findChild(QObject,'androidRomPath').property('visible');assert w.findChild(QObject,'combinedDefaultChoice').property('visible')
 assert w.findChild(QObject,'androidWriteScope').property('visible');capture('combined-materials')
 c.prepareWorkflow();wait(lambda:c.data['state']=='prepared' and not c.data['busy']);plan=c.data['wizardPlan']
 assert plan['kind']=='combined' and len(plan['installations'])==3 and plan['default_target']==second
 assert len([j for j in plan['installations'] if j['set_default']])==1;capture('combined-plan')
 c.executeWorkflow(c.data['selectedSerial'],True,True);wait(lambda:c.data['state']=='wizard_complete' and not c.data['busy'])
 assert c.data['receipt']['installed_targets'][-1]==second;assert len(c.data['receipt']['children'])==3
 c.scanDevice();wait(lambda:c.data['inventorySummary'] and not c.data['busy']);c.selectPartition(first);c.setInstallTarget(second,True)
 c.setMaterial('android','');c.setDefaultTarget(first);c.nextFromPartitions();assert c.data['targetKind']=='combined'
 QCoreApplication.processEvents();assert not w.findChild(QObject,'androidRomPath').property('visible')
 c.prepareWorkflow();wait(lambda:c.data['state']=='prepared' and not c.data['busy']);assert not c.data['wizardPlan']['rom'] and not c.data['wizardPlan']['android_writes']
 assert len(c.data['wizardPlan']['installations'])==2 and c.data['wizardPlan']['default_target']==first
 c.back(0);wait(lambda:c.data['state']=='cancelled' and not c.data['busy'])
 c.selectDevice(c.data['selectedSerial']);assert not c.data['installTargets'] and not c.data['linuxBundles'] and not c.data['defaultTarget']
 wait(lambda:not c.data['busy']);c.process.closeWriteChannel();c.process.waitForFinished(5000);qml.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
print('PASS: partition-first Android + two Linux selection, individual sources, final default, unified confirmation/execution, Linux-only without ROM, selection reset; demo-only')
