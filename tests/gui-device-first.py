#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import os
os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['QT_QUICK_BACKEND']='software'
import argparse
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QObject,QUrl,QEvent,QMetaObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT/'installer'))
from liuqin_gui.wizard import WizardController
parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
QQuickStyle.setStyle('Basic');app=QGuiApplication([sys.argv[0]])
def wait(predicate,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        QCoreApplication.processEvents()
        if predicate():return
        QTest.qWait(10)
    raise AssertionError('timeout: '+controller.data['error'])

def visual_child(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_child(child, name)
        if found is not None:
            return found

with tempfile.TemporaryDirectory() as directory:
    controller=WizardController(True,None,Path(directory));qml=QQmlApplicationEngine()
    qml.rootContext().setContextProperty('backend',controller)
    qml.load(QUrl.fromLocalFile(str(PROJECT/'installer/liuqin_gui/qml/Main.qml')))
    assert qml.rootObjects(),'new QML failed';window=qml.rootObjects()[0]
    wait(lambda:controller.data['environment'] and not controller.data['busy'])
    assert controller.data['compatibility']['ready']
    assert controller.data['inventorySummary']=={},'connecting must not boot the installation environment'
    assert window.findChild(QObject,'inspectEnvironmentButton') is not None
    assert '演示 MIUI' in window.findChild(QObject,'environmentLabel').property('text')
    tools=window.findChild(QObject,'advancedTools')
    tools_toggle=window.findChild(QObject,'advancedToolsToggle')
    assert not tools.property('visible')
    tools_toggle.setProperty('checked',True)
    assert tools.property('visible')
    assert window.findChild(QObject,'inspectEnvironmentButton').property('visible')
    assert tools.findChild(QObject,'resumePartitionInit') is None
    assert window.findChild(QObject,'useNativeInstaller') is None
    tools_toggle.setProperty('checked',False)
    if args.screenshots:
        args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'connection.png'))
    compatible=controller.data['compatibility']
    controller.update(compatibility=dict(ready=False,reason='已识别 MIUI 14.0.3 · Android 13，但没有匹配的 Recovery 验收记录。保留当前系统，不自动重启。'))
    assert not window.findChild(QObject,'readDeviceButton').property('enabled')
    if args.screenshots:
        QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'unsupported.png'))
    controller.update(compatibility=compatible)
    controller.scanDevice()
    wait(lambda:controller.data['inventorySummary'] and not controller.data['busy'])
    assert controller.data['wizardStep']==0
    create_partition=window.findChild(QObject,'newPartitionButton')
    partition_dialog=window.findChild(QObject,'newPartitionDialog')
    partition_ok=window.findChild(QObject,'newPartitionOk')
    use_all=window.findChild(QObject,'newPartitionUseAll')
    assert QMetaObject.invokeMethod(create_partition,'click')
    wait(lambda:partition_dialog.property('opened'))
    assert not partition_ok.property('enabled') and not use_all.property('enabled')
    assert QMetaObject.invokeMethod(partition_dialog,'reject')
    wait(lambda:not partition_dialog.property('visible'))
    rows=window.findChild(QObject,'partitionRows')
    all_partitions=window.findChild(QObject,'allPartitionsToggle')
    assert rows.property('count')==sum(r['role'] in ('android','linux') for r in controller.data['partitionRows'])
    all_partitions.setProperty('checked',True)
    assert rows.property('count')==len(controller.data['partitionRows'])
    all_partitions.setProperty('checked',False)
    assert not window.findChild(QObject,'partitionNext').property('enabled')
    if args.screenshots:
        controller.selectPartition('userdata')
        QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'partition-targets.png'))
        tools_toggle.setProperty('checked',True)
        QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'advanced-tools.png'))
        tools_toggle.setProperty('checked',False)
        controller.selectPartition('')
    assert window.findChild(QObject,'recoveryModeChoice') is None
    assert window.findChild(QObject,'recoveryImagePath') is None
    assert controller.data['linuxBundle']=='' and controller.data['romPath']==''
    assert any(p['name']=='userdata' for p in controller.data['partitionRows'])
    controller.update(inspectionOnly=True)
    assert not window.findChild(QObject,'partitionNext').property('enabled')
    controller.selectPartition('userdata');controller.nextFromPartitions()
    assert controller.data['wizardStep']==1 and controller.data['romPreviewMode']
    assert not window.findChild(QObject,'executeButton').property('visible')
    controller.back(0)
    controller.update(inspectionOnly=False,error='')
    controller.selectPartition('userdata');controller.nextFromPartitions()
    assert controller.data['wizardStep']==1 and controller.data['targetKind']=='android'
    controller.back(0)
    controller.editPartition('userdata','delete','')
    controller.createPartition('android',32,'f2fs','Android')
    capacity=controller.partitionCapacity('linux')
    controller.createPartition('linux',capacity['max_gib']+1,'ext4','Oversized')
    assert not controller.data['newRoots'] and controller.data['error']
    assert QMetaObject.invokeMethod(create_partition,'click')
    wait(lambda:partition_dialog.property('opened'))
    size_box=window.findChild(QObject,'newPartitionSize')
    assert size_box.property('to')==capacity['max_gib']
    size_input=size_box.property('contentItem')
    size_input.setProperty('text',str(capacity['max_gib']+1))
    assert not partition_ok.property('enabled')
    size_input.setProperty('text','32')
    assert partition_ok.property('enabled')
    kind_box=window.findChild(QObject,'newPartitionKind')
    kind_box.setProperty('currentIndex',1)
    assert not partition_ok.property('enabled') and not use_all.property('enabled')
    kind_box.setProperty('currentIndex',0)
    assert QMetaObject.invokeMethod(use_all,'click')
    assert partition_dialog.property('useAll') and partition_ok.property('enabled')
    if args.screenshots:
        QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'partition-use-all.png'))
    assert QMetaObject.invokeMethod(partition_ok,'click')
    wait(lambda:not partition_dialog.property('visible'))
    assert controller.data['newRoots'][0]['size_bytes']==capacity['max_bytes']
    selected=controller.data['selectedPartition'];assert selected.startswith('lqr_')
    controller.setInstallTarget(selected, False)
    next_button = window.findChild(QObject, 'partitionNext')
    assert next_button.property('enabled') and '分区方案' in next_button.property('text')
    assert QMetaObject.invokeMethod(next_button, 'click')
    controller.prepareWorkflow()
    wait(lambda:not controller.data['busy'] and controller.data['error'])
    assert '默认启动' in controller.data['error']
    assert not controller.data['wizardPlan']
    controller.back(0)
    assert controller.data['wizardStep']==0
    controller.setInstallTarget(selected, True)
    controller.nextFromPartitions();assert controller.data['targetKind']=='linux'
    controller.setMaterial('linux',str(Path(directory)/'demo-materials'))
    if args.screenshots:
        args.screenshots.mkdir(parents=True,exist_ok=True);QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'materials.png'))
    controller.prepareWorkflow();wait(lambda:controller.data['state']=='prepared' and not controller.data['busy'])
    assert len(controller.data['wizardPlan']['additions'])==3, (controller.data['wizardPlan'], controller.partition_configuration())
    assert next(p for p in controller.data['wizardPlan']['additions'] if p['name']==selected)['bytes']==capacity['max_bytes']
    assert any(p['action']=='删除' and p['name']=='userdata' for p in controller.data['wizardPlan']['destructive'])
    if args.screenshots:
        QTest.qWait(100);window.grabWindow().save(str(args.screenshots/'summary.png'))
    controller.executeWorkflow('DEMO-LIUQIN',True,True)
    wait(lambda:controller.data['state']=='wizard_complete' and not controller.data['busy'])
    assert controller.data['receipt']['partitions_edited'] and controller.data['receipt']['system_installed']
    completed_receipt=controller.data['receipt']
    resume=window.findChild(QObject,'resumePartitionInit')
    assert resume is not None and not resume.property('visible')
    controller.update(receipt=dict(failed=True,write_dispatched=False))
    assert not resume.property('visible')
    controller.update(receipt=dict(failed=True,write_dispatched=True))
    assert resume.property('visible')
    controller.update(receipt=completed_receipt)
    controller.scanDevice();wait(lambda:controller.data['inventorySummary'] and not controller.data['busy'])
    row=next(p for p in controller.data['partitionRows'] if p['name']==selected)
    assert row['status']=='installed' and row['can_format'] and row['can_delete']
    # Exercise the switch UI with a registered Android supplied by a fixture.
    # The intercepted request never reaches the installation worker.
    system_id = 'fe36d332-f536-4d76-998f-c674a32242ca'
    inventory = controller.data['inventorySummary']
    runtime_path = controller.data['runtimePath']
    controller.handle_event(dict(type='wizard_inventory', partitions=controller.data['partitionRows'],
        summary=inventory, runtime_path=runtime_path, switch_systems=[
            dict(id=row['system_id'], name='Fedora Plasma', type='linux', partition=selected, is_default=True),
            dict(id=system_id, name='Android', type='android', partition='userdata', is_default=False),
        ]))
    target_name = 'switchDefault_'+system_id
    wait(lambda:visual_child(window.contentItem(),target_name) is not None)
    switch = visual_child(window.contentItem(),target_name)
    assert window.findChild(QObject,'defaultSwitchCard').property('visible') and switch.property('enabled')
    assert not visual_child(window.contentItem(),'switchDefault_'+row['system_id']).property('visible')
    if args.screenshots:
        QTest.qWait(100)
        window.grabWindow().save(str(args.screenshots/'default-systems.png'))
    controller.editPartition(selected,'format','ext4')
    assert not switch.property('enabled')
    controller.undoEdit(selected)
    assert switch.property('enabled')
    with patch.object(controller,'send') as send:
        assert QMetaObject.invokeMethod(switch,'clicked')
        send.assert_called_once_with('wizard_switch_prepare',serial='DEMO-LIUQIN',runtime_path=runtime_path,
            snapshot_id=inventory['snapshot_id'],system_id=system_id)
        assert controller.data['wizardStep']==2 and controller.data['bootSwitchMode']
        controller.handle_event(dict(type='state',state='prepared',can_cancel=False))
        controller.handle_event(dict(type='wizard_plan',plan=dict(kind='switch',target='Android',
            previous_name='Fedora Plasma',serial='DEMO-LIUQIN',plan_id='demo-switch',backup_dir=directory)))
        consent=window.findChild(QObject,'operationConsent')
        execute=window.findChild(QObject,'executeButton')
        assert consent.property('text')=='我确认切换下次开机的默认启动系统'
        assert execute.property('text')=='确认切换' and not execute.property('enabled')
        if args.screenshots:
            QTest.qWait(100)
            window.grabWindow().save(str(args.screenshots/'default-switch-confirmation.png'))
        consent.setProperty('checked',True)
        assert execute.property('enabled')
        assert QMetaObject.invokeMethod(execute,'clicked')
        send.assert_called_with('wizard_install',session_id=controller.session_id,plan_id='demo-switch',
            confirmation=dict(serial='DEMO-LIUQIN',accept_operations=True,allow_experimental=True))
        assert QMetaObject.invokeMethod(window.findChild(QObject,'wizardBack'),'clicked')
        send.assert_called_with('cancel')
        controller.handle_event(dict(type='state',state='cancelled',can_cancel=False))
        assert controller.data['wizardStep']==0 and not controller.data['bootSwitchMode']
    controller.selectPartition(selected);controller.nextFromPartitions()
    controller.prepareWorkflow();wait(lambda:controller.data['state']=='prepared' and not controller.data['busy'])
    assert any(p['action'].startswith('格式化') for p in controller.data['wizardPlan']['destructive'])
    controller.back(0);wait(lambda:controller.data['state']=='cancelled' and not controller.data['busy'])
    controller.process.closeWriteChannel();controller.process.waitForFinished(5000)
    qml.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);QCoreApplication.processEvents()
print('PASS: device scan before materials, Linux install and reinstall, independent default switch confirmation and cancel')
