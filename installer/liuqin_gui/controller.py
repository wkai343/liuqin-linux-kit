# SPDX-License-Identifier: MIT
"""Shared worker lifecycle, diagnostics and write-aware window closing."""
import json
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QObject, Property, QProcess, QProcessEnvironment, QTimer, QUrl, Signal, Slot


class Controller(QObject):
    changed = Signal()
    quitRequested = Signal()

    def __init__(self, demo, state_dir, parent=None):
        super().__init__(parent)
        self.state_dir = Path(state_dir).resolve()
        self.demo = demo
        self.close_after_cancel = False
        self.buffer = bytearray()
        self.session_id = ''
        self.data = dict(state='idle', phase='', message='连接平板后读取分区。',
                         progress=-1.0, busy=False, workerReady=False, simulation=demo,
                         backupPath=str(self.state_dir / 'backups'), devices=[], selectedSerial='',
                         device={}, receipt={}, log='', error='', canCancel=False)
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
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PYTHONUTF8', '1')
        environment.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(environment)
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
    def setBackupPath(self, path):
        if not self.configEditable():
            return
        url = QUrl(path)
        self.update(backupPath=url.toLocalFile() if url.isLocalFile() else path)

    def configEditable(self):
        return not self.data['busy'] and self.data['state'] != 'prepared'

    @Slot()
    def cancel(self):
        if self.data['state'] in ('preparing', 'backing_up', 'prepared'):
            self.send('cancel')

    @Slot(str)
    def exportReport(self, directory):
        path = QUrl(directory)
        destination = Path(path.toLocalFile() if path.isLocalFile() else directory)
        try:
            report = dict(simulation=self.demo, state=self.data['state'], receipt=self.data['receipt'],
                          error=self.data['error'], phases=self.data['log'])
            text = json.dumps(report, ensure_ascii=False, indent=2)
            paths = [(self.data['selectedSerial'], '<device-serial>'),
                     (self.data.get('wizardPlan', {}).get('backup_dir', ''), '<private-backup>'),
                     (self.data['receipt'].get('backup_dir', ''), '<private-backup>'),
                     (self.data['receipt'].get('run_dir', ''), '<private-run>'),
                     (self.data.get('linuxBundle', ''), '<bundle>'),
                     (self.data.get('runtimePath', ''), '<recovery-image>')]
            paths.extend((value, '<bundle>') for value in self.data.get('linuxBundles', {}).values())
            paths.append((str(Path.home()), '<home>'))
            for value, replacement in paths:
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
        elif kind == 'devices':
            devices = event['devices']
            selected = self.data['selectedSerial']
            if selected not in [d['serial'] for d in devices]:
                selected = devices[0]['serial'] if devices else ''
            self.data.update(devices=devices, selectedSerial=selected)
        elif kind == 'device':
            self.data['device'] = event['device']
        elif kind == 'backup_ready' and event['backup']['serial'] == self.data['selectedSerial']:
            self.append_log('设备备份已新建并核验。' if event['backup']['source'] == 'created' else '已有设备备份校验通过，可复用。')
        elif kind == 'backup_rejected':
            self.append_log('跳过未通过核验的备份：' + event['message'])
        elif kind == 'state':
            self.data.update(state=event['state'], canCancel=event['can_cancel'])
            if event['state'] == 'wizard_complete':
                self.data['error'] = ''
            elif event['state'] == 'cancelled':
                self.data['message'] = '操作已取消。'
        elif kind == 'phase':
            self.data.update(phase=event['phase'], message=event['message'], progress=-1.0)
            self.append_log(event['message'] + (' · ' + event['detail'] if event.get('detail') else ''))
        elif kind == 'notice':
            self.data.update(message=event['message'], progress=-1.0)
            self.append_log(event['message'])
        elif kind == 'progress' and event['phase'] == self.data['phase']:
            self.data['progress'] = event.get('progress', -1.0)
        elif kind == 'error':
            self.data.update(error=event['message'], receipt=event.get('receipt', {}))
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
