#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offscreen QML wizard integration test with the isolated demo worker."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, QProcess, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / 'installer'))
from liuqin_gui.controller import Controller

parser = argparse.ArgumentParser()
parser.add_argument('--screenshots', type=Path)
args = parser.parse_args()
QQuickStyle.setStyle('Basic')
app = QGuiApplication([sys.argv[0]])


def wait(condition, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if condition():
            return
        QTest.qWait(10)
    raise AssertionError('timed out waiting for GUI/worker')


with tempfile.TemporaryDirectory() as directory:
    controller = Controller(True, None, Path(directory))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('backend', controller)
    engine.load(QUrl.fromLocalFile(str(project / 'installer/liuqin_gui/qml/Main.qml')))
    assert engine.rootObjects(), 'QML did not load'
    window = engine.rootObjects()[0]

    def capture(name):
        QTest.qWait(120)
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            assert window.grabWindow().save(str(args.screenshots / name))

    wait(lambda: controller.data['workerReady'] and not controller.data['busy'])
    assert controller.data['devices'][0]['serial'] == 'DEMO-LIUQIN'
    controller.validateBundle()
    wait(lambda: controller.data['bundleValid'] and not controller.data['busy'])
    capture('QML安装助手-准备.png')
    controller.goPage(1)
    capture('QML安装助手-设备.png')
    prepare_button = window.findChild(QObject, 'prepareButton')
    assert prepare_button and not prepare_button.property('enabled'), 'installation preparation allowed without backup'
    controller.createBackup(False)
    wait(lambda: controller.data['backupInfo'].get('valid_local') and not controller.data['busy'])
    original_backup = controller.data['selectedBackupPath']
    capture('QML安装助手-备份.png')
    assert prepare_button.property('enabled')
    controller.createBackup(False)
    wait(lambda: controller.data['backupInfo'].get('source') == 'reused' and not controller.data['busy'])
    assert controller.data['selectedBackupPath'] == original_backup
    controller.setFilesystem('btrfs')
    assert controller.data['filesystem']=='btrfs'
    controller.prepare()
    wait(lambda: controller.data['state'] == 'prepared' and not controller.data['busy'])
    assert not controller.data['receipt'].get('root_installed', False)
    assert controller.data['plan']['backup_reused']
    assert controller.data['plan']['backup_dir'] == original_backup
    assert controller.data['plan']['filesystem']=='btrfs'
    controller.setFilesystem('ext4')
    assert controller.data['filesystem']=='btrfs', 'prepared plan must freeze filesystem selection'
    capture('QML安装助手-方案.png')
    original_plan = dict(controller.data['plan'])
    controller.update(plan=dict(original_plan, warnings=['当前设备状态与原始恢复备份不完全相同。原始备份仍有效并保留；本次使用已检查的当前设备校准数据。']))
    assert controller.data['state'] == 'prepared'
    capture('QML安装助手-备份差异提示.png')
    controller.update(plan=original_plan)
    controller.goPage(3)
    button = window.findChild(QObject, 'installButton')
    serial = window.findChild(QObject, 'confirmationSerial')
    assert button and serial and not button.property('enabled')
    for name in ('backedUpCheck', 'eraseCheck', 'experimentalCheck'):
        window.findChild(QObject, name).setProperty('checked', True)
    serial.setProperty('text', 'WRONG')
    QTest.qWait(30)
    assert not button.property('enabled'), 'wrong serial allowed installation'
    serial.setProperty('text', 'DEMO-LIUQIN')
    wait(lambda: button.property('enabled'))
    capture('QML安装助手-确认.png')
    QMetaObject.invokeMethod(button, 'clicked', Qt.ConnectionType.DirectConnection)
    wait(lambda: controller.data['state'] == 'installing')
    assert not controller.closeWindow(), 'window closed during installation'
    wait(lambda: controller.data['state'] == 'awaiting_first_boot' and not controller.data['busy'])
    assert controller.data['receipt']['boot_readback_verified']
    assert not controller.data['receipt']['first_boot_verified']
    assert controller.data['page'] == 5
    capture('QML安装助手-结果.png')
    controller.verifyFirstBoot()
    wait(lambda: controller.data['state'] == 'verified' and not controller.data['busy'])
    assert controller.data['receipt']['simulation']
    assert controller.closeWindow()
    wait(lambda: controller.process.state() == QProcess.ProcessState.NotRunning)
    window.close()
print('PASS: QML wizard, bound confirmation, demo installation and first-boot verification')
