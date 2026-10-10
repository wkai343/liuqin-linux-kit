# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import re
import sys
import time
import uuid

from PySide6.QtCore import QObject, Property, QProcess, QTimer, QUrl, Signal, Slot


class Controller(QObject):
    changed = Signal()
    quitRequested = Signal()

    def __init__(self, demo, bundle, state_dir, parent=None):
        super().__init__(parent)
        self.state_dir = Path(state_dir).resolve()
        self.demo = demo
        self.close_after_cancel = False
        self.pending_backup_lookup = False
        self.buffer = bytearray()
        self.session_id = ''
        self.data = dict(page=0, state='idle', phase='', message='选择安装材料，开始检查。',
                         progress=-1.0, busy=False, workerReady=False, simulation=demo,
                         bundlePath='内置演示安装包' if demo else str(bundle or ''),
                         backupPath=str(self.state_dir / 'backups'),
                         selectedBackupPath='', backupInfo={}, backupCandidates=[],
                         devices=[], selectedSerial='', bundleInfo={}, bundleValid=False,
                         plan={}, device={}, receipt={}, log='', error='', canCancel=False)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        arguments = ['-u', str(Path(__file__).resolve().parents[1] / 'install-gui.py'),
                     '--worker', '--state-dir', str(self.state_dir)]
        if demo:
            arguments.append('--demo')
        self.process.setProgram(sys.executable)
        self.process.setArguments(arguments)
        self.process.start()

    @Property('QVariantMap', notify=changed)
    def view(self):
        return self.data

    def update(self, **values):
        self.data.update(values)
        self.changed.emit()

    def append_log(self, text):
        lines = (self.data['log'] + '\n' + text).strip().splitlines()
        self.data['log'] = '\n'.join(lines[-300:])

    def send(self, action, **values):
        if not self.data['workerReady'] or self.process.state() != QProcess.ProcessState.Running:
            self.update(error='安装后端尚未就绪。')
            return
        request = dict(id=uuid.uuid4().hex, action=action, **values)
        if action not in ('cancel', 'shutdown'):
            self.update(busy=True)
        self.process.write((json.dumps(request, ensure_ascii=False) + '\n').encode())

    @Slot()
    def probeDevices(self):
        if self.data['busy']:
            return
        self.update(error='')
        self.send('devices')

    @Slot(str)
    def setBundlePath(self, path):
        if self.data['busy'] or self.data['state'] == 'prepared':
            return
        url = QUrl(path)
        value = url.toLocalFile() if url.isLocalFile() else path
        self.update(bundlePath=value, bundleValid=False, bundleInfo={}, error='')

    @Slot(str)
    def setBackupPath(self, path):
        if self.data['busy'] or self.data['state'] == 'prepared':
            return
        url = QUrl(path)
        self.update(backupPath=url.toLocalFile() if url.isLocalFile() else path)
        if self.data['selectedSerial']:
            self.findExistingBackup()

    @Slot()
    def findExistingBackup(self):
        if self.data['busy'] or not self.data['selectedSerial']:
            return
        self.send('find_backup', serial=self.data['selectedSerial'], backup_parent=self.data['backupPath'])

    @Slot(str)
    def selectBackup(self, path):
        if self.data['busy'] or self.data['state'] == 'prepared':
            return
        url = QUrl(path)
        directory = url.toLocalFile() if url.isLocalFile() else path
        self.update(error='', selectedBackupPath=directory, backupInfo={})
        self.send('select_backup', serial=self.data['selectedSerial'], backup_dir=directory)

    @Slot(bool)
    def createBackup(self, force_new=False):
        if self.data['busy'] or not self.data['selectedSerial']:
            return
        self.update(error='', page=1)
        self.send('backup', serial=self.data['selectedSerial'], bundle=self.data['bundlePath'],
                  backup_parent=self.data['backupPath'], force_new=force_new)

    @Slot()
    def validateBundle(self):
        if self.data['busy']:
            return
        self.update(error='', bundleValid=False)
        self.send('validate', bundle=self.data['bundlePath'])

    @Slot(str)
    def selectDevice(self, serial):
        if not self.data['busy'] and self.data['state'] != 'prepared':
            self.update(selectedSerial=serial, selectedBackupPath='', backupInfo={})
            self.findExistingBackup()

    @Slot(int)
    def goPage(self, page):
        if self.data['busy']:
            return
        state = self.data['state']
        if state == 'prepared' and page not in (2, 3):
            self.update(error='请先确认方案或取消准备。')
            return
        if page == 1 and not self.data['bundleValid']:
            self.update(error='请先通过安装包检查。')
            return
        if page in (2, 3) and state != 'prepared':
            return
        if page in (4, 5):
            return
        self.update(page=page, error='')

    @Slot()
    def prepare(self):
        if self.data['busy'] or not self.data['selectedSerial']:
            self.update(error='请先选择连接的平板。')
            return
        if not self.data['backupInfo'].get('valid_local') or not self.data['selectedBackupPath']:
            self.update(error='请先独立创建设备备份或选择已有备份。')
            return
        self.update(page=2, plan={}, device={}, receipt={}, progress=-1.0, error='')
        self.send('prepare', serial=self.data['selectedSerial'],
                  bundle=self.data['bundlePath'], backup_dir=self.data['selectedBackupPath'], layout='linux-single')

    @Slot(str, bool, bool, bool)
    def install(self, serial, backed_up, erase, experimental):
        if self.data['busy'] or self.data['state'] != 'prepared':
            return
        self.update(error='')
        self.send('install', session_id=self.session_id, plan_id=self.data['plan'].get('plan_id', ''),
                  confirmation=dict(serial=serial.strip(), personal_data_backed_up=backed_up,
                                    erase_userdata=erase, allow_experimental=experimental))

    @Slot()
    def cancel(self):
        if self.data['state'] in ('preparing', 'backing_up', 'prepared'):
            self.send('cancel')

    @Slot()
    def verifyFirstBoot(self):
        if not self.data['busy']:
            self.send('verify', session_id=self.session_id, desktop_and_reboot_verified=True)

    @Slot(str)
    def exportReport(self, directory):
        path = QUrl(directory)
        destination = Path(path.toLocalFile() if path.isLocalFile() else directory)
        try:
            report = dict(simulation=self.demo, state=self.data['state'], receipt=self.data['receipt'],
                          error=self.data['error'], phases=self.data['log'])
            text = json.dumps(report, ensure_ascii=False, indent=2)
            for value, replacement in ((self.data['selectedSerial'], '<device-serial>'),
                                       (self.data['plan'].get('backup_dir', ''), '<private-backup>'),
                                       (self.data['receipt'].get('backup_dir', ''), '<private-backup>'),
                                       (self.data['receipt'].get('run_dir', ''), '<private-run>'),
                                       (self.data['bundlePath'], '<bundle>'), (str(Path.home()), '<home>')):
                if value:
                    text = text.replace(value, replacement)
            target = destination / ('liuqin-report-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
            with target.open('x', encoding='utf-8') as stream:
                stream.write(text + '\n')
            self.append_log('诊断报告已保存：' + str(target))
            self.changed.emit()
        except Exception as exc:
            self.update(error='报告保存失败：' + str(exc))

    @Slot(result=bool)
    def closeWindow(self):
        if self.data['busy'] or self.data['state'] == 'installing':
            self.update(error='当前操作仍在进行。请等待结束；准备阶段可点击取消。')
            return False
        if self.data['state'] == 'prepared':
            self.close_after_cancel = True
            self.cancel()
            return False
        self.send('shutdown')
        self.process.closeWriteChannel()
        return True

    def read_output(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b'\n' in self.buffer:
            line, _, rest = self.buffer.partition(b'\n')
            self.buffer[:] = rest
            try:
                self.handle_event(json.loads(line))
            except (ValueError, TypeError) as exc:
                self.update(error='后端事件格式异常：' + str(exc))

    def handle_event(self, event):
        kind = event.get('type')
        if event.get('session_id'):
            self.session_id = event['session_id']
        if kind == 'ready':
            self.update(workerReady=True)
            QTimer.singleShot(0, self.probeDevices)
            return
        if kind == 'activity':
            self.data['busy'] = event['busy']
            if not event['busy'] and self.close_after_cancel and self.data['state'] == 'cancelled':
                self.send('shutdown')
                self.process.closeWriteChannel()
                self.quitRequested.emit()
            elif not event['busy'] and self.pending_backup_lookup:
                self.pending_backup_lookup = False
                self.findExistingBackup()
        elif kind == 'devices':
            devices = event['devices']
            selected = self.data['selectedSerial']
            if selected not in [d['serial'] for d in devices]:
                selected = devices[0]['serial'] if devices else ''
                self.data.update(selectedBackupPath='', backupInfo={})
            self.data.update(devices=devices, selectedSerial=selected)
            self.pending_backup_lookup = bool(selected)
        elif kind == 'bundle':
            self.data.update(bundleValid=True, bundleInfo=event, error='')
        elif kind == 'device':
            self.data['device'] = event['device']
        elif kind == 'backups':
            self.data['backupCandidates'] = event['candidates']
        elif kind == 'backup_ready' and event['backup']['serial'] == self.data['selectedSerial']:
            info = event['backup']
            self.data.update(backupInfo=info, selectedBackupPath=info['path'], error='')
            self.append_log('设备备份已新建并核验。' if info['source'] == 'created' else '已有设备备份校验通过，可复用。')
        elif kind == 'backup_unavailable' and event['serial'] == self.data['selectedSerial']:
            self.data.update(selectedBackupPath='', backupInfo={})
        elif kind == 'backup_rejected':
            self.append_log('跳过未通过核验的备份：' + event['message'])
        elif kind == 'plan_ready':
            self.data.update(plan=event['plan'], page=2, progress=-1.0,
                             message='真实方案已准备好，复用已有设备备份。', error='')
        elif kind == 'state':
            self.data.update(state=event['state'], canCancel=event['can_cancel'])
            if event['state'] == 'installing':
                self.data['page'] = 4
            elif event['state'] in ('awaiting_first_boot', 'verified'):
                self.data['page'] = 5
                self.data['error'] = ''
            elif event['state'] == 'cancelled':
                self.data.update(page=1, plan={}, message='操作已取消，未下发格式化。')
            elif event['state'] == 'backup_complete':
                self.data.update(page=1, message='设备备份已完成，平板已返回 Fastboot。', error='')
        elif kind == 'phase':
            self.data.update(phase=event['phase'], message=event['message'], progress=-1.0)
            self.append_log(event['message'] + (' · ' + event['detail'] if event.get('detail') else ''))
        elif kind == 'progress' and event['phase'] == self.data['phase']:
            self.data['progress'] = event.get('progress', -1.0)
        elif kind in ('installation_written', 'verified'):
            self.data['receipt'] = event['receipt']
        elif kind == 'error':
            self.data.update(error=event['message'], receipt=event.get('receipt', {}))
            if event.get('writes_may_have_started'):
                self.data['page'] = 5
            elif event.get('phase') == 'verify_backup':
                self.data['backupInfo'] = {}
            self.append_log('停止：' + event['message'])
        elif kind in ('request_rejected', 'cancel_requested'):
            self.data['error'] = event.get('message', '')
        self.changed.emit()

    def read_error(self):
        text = bytes(self.process.readAllStandardError()).decode(errors='replace').strip()
        if text:
            self.append_log(text[-2000:])
            self.changed.emit()

    def finished(self, exit_code, _exit_status):
        self.data['workerReady'] = False
        self.data['busy'] = False
        if exit_code or self.data['state'] in ('preparing', 'backing_up', 'prepared', 'installing'):
            self.update(error='后端已退出。保留现有备份和记录，先检查平板状态。')
        else:
            self.changed.emit()

    def process_error(self, _error):
        self.update(error='无法启动安装后端：' + self.process.errorString(), workerReady=False)
