#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise boot-store review and consent in the actual QML page."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QUICK_BACKEND']='software'
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QMetaObject,QObject,QUrl,QProcess
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.wizard import WizardController


def wait(predicate):
    end=time.monotonic()+30
    while time.monotonic()<end:
        QCoreApplication.processEvents()
        if predicate():return
        QTest.qWait(10)
    raise AssertionError('GUI timeout')


def visual(item,name):
    if item.objectName()==name:return item
    for child in item.childItems():
        found=visual(child,name)
        if found:return found


QQuickStyle.setStyle('Basic');app=QGuiApplication([sys.argv[0]])
with tempfile.TemporaryDirectory() as directory:
    controller=WizardController(True,None,Path(directory));qml=QQmlApplicationEngine()
    qml.rootContext().setContextProperty('backend',controller)
    qml.load(QUrl.fromLocalFile(str(PROJECT/'installer/liuqin_gui/qml/Main.qml')))
    assert qml.rootObjects();window=qml.rootObjects()[0]
    wait(lambda:controller.data['environment'] and not controller.data['busy'])
    entry=window.findChild(QObject,'bootStoreEntry');assert entry is not None
    runtime=controller.data['runtimePath']
    with patch.object(controller,'send') as send:
        controller.inspectBootStore()
        send.assert_called_once_with('boot_store_inspect',serial='DEMO-LIUQIN',runtime_path=runtime)
    transaction='switch-11111111-1111-4111-8111-111111111111'
    report=dict(available=True,snapshot_id='a'*64,total_bytes=2*1024**3,free_bytes=1024**3,
                allocated_bytes=512*1024**2,reclaimable_bytes=128*1024**2,default_name='Fedora',
                default_matches=False,pending_count=1,transactions=[dict(name=transaction,status='pending',
                    message='旧引导快照和分区身份已核验。',actions=[dict(operation='restore-switch',label='恢复原默认引导')])])
    controller.handle_event(dict(type='boot_store_report',report=report))
    assert window.findChild(QObject,'bootStoreCard').property('visible')
    assert not window.findChild(QObject,'prepareStoreCleanup').property('enabled')
    cleanup=window.findChild(QObject,'prepareStoreCleanup')
    resumable=dict(report,reclaimable_bytes=0,default_matches=True,pending_count=0,transactions=[],
                   cleanup_available=True,cleanup_pending=True,retention=dict(units=[dict(path='old-record')]))
    controller.handle_event(dict(type='boot_store_report',report=resumable))
    assert cleanup.property('enabled')
    assert cleanup.property('text')=='继续清理'
    with patch.object(controller,'send') as send:
        QMetaObject.invokeMethod(cleanup,'clicked')
        send.assert_called_once_with('boot_store_prepare',serial='DEMO-LIUQIN',runtime_path=runtime,
            operation='cleanup',transaction='',snapshot_id='a'*64)
    controller.update(wizardStep=0,busy=False)
    # Previously shipped retention Rec reports expose units but no availability flag.
    legacy=dict(resumable);legacy.pop('cleanup_available');legacy.pop('cleanup_pending')
    controller.handle_event(dict(type='boot_store_report',report=legacy))
    assert cleanup.property('enabled')
    for change in (dict(cleanup_available=False),dict(pending_count=1),dict(default_matches=False),
                   dict(retention_error='changed material'),dict(retention=dict(units=[]))):
        controller.handle_event(dict(type='boot_store_report',report=dict(legacy,**change)))
        assert not cleanup.property('enabled'),change
    controller.handle_event(dict(type='boot_store_report',report=dict(legacy,retention=None,reclaimable_bytes=4096)))
    assert cleanup.property('enabled')
    controller.handle_event(dict(type='boot_store_report',report=report))
    wait(lambda:visual(window.contentItem(),'storeAction_restore-switch') is not None)
    with patch.object(controller,'send') as send:
        QMetaObject.invokeMethod(visual(window.contentItem(),'storeAction_restore-switch'),'clicked')
        send.assert_called_once_with('boot_store_prepare',serial='DEMO-LIUQIN',runtime_path=runtime,
            operation='restore-switch',transaction=transaction,snapshot_id='a'*64)
    assert controller.data['bootStoreMode'] and controller.data['wizardStep']==2
    controller.handle_event(dict(type='wizard_plan',plan=dict(kind='maintenance',plan_id='b'*64,
        serial='DEMO-LIUQIN',target='恢复原默认引导',maintenance_message='仅恢复引导与登记。',backup_dir=directory)))
    controller.update(state='prepared',busy=False)
    consent=window.findChild(QObject,'operationConsent')
    assert '中断恢复' in consent.property('text')
    assert not window.findChild(QObject,'executeButton').property('enabled')
    consent.setProperty('checked',True)
    assert window.findChild(QObject,'executeButton').property('enabled')
    controller.update(state='wizard_scanned')
    with patch.object(controller,'send'):
        controller.selectDevice('OTHER')
    assert not controller.data['bootStoreReport']
    controller.update(busy=False);controller.closeWindow()
    wait(lambda:controller.process.state()==QProcess.ProcessState.NotRunning)
print('PASS: boot-store inspection, recovery action binding and explicit maintenance consent')
