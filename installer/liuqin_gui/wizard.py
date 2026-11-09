# SPDX-License-Identifier: MIT
"""Device-first presentation model, independent of installer backend modes."""
import uuid
from liuqin_layout import GIB
from liuqin_partition_names import root_name, is_root_name
from liuqin_partitions import addition_capacity
from PySide6.QtCore import Slot,QUrl,QTimer
from .controller import Controller
from .runtime import locate,backend_hint


class WizardController(Controller):
    def __init__(self,demo,bundle,state_dir,parent=None,installer_image=''):
        super().__init__(demo,state_dir,parent)
        self._auto_inspect=True;self._back_to=1
        self.data.update(wizardStep=0,runtimePath=locate(installer_image,str(bundle or ''),state_dir),partitionRows=[],
            inventorySummary={},selectedPartition='',targetKind='',installTargets=[],linuxBundles={},defaultTarget='',editActions=[],newRoots=[],newUserdata=0,newUserdataBytes=0,
            linuxBundle=str(bundle or ''),romPath='',fixedRecoveryPolicy='auto',setDefault=True,wizardPlan={},
            switchSystems=[],bootSwitchMode=False,bootStoreMode=False,bootStoreReport={},
            environment={},compatibility={},firmwareSummary='',firmwareSlots='',firmwareRecordPath='',inspectionOnly=False,nativeLogPath='',
            romPreviewMode=False,romPreview={},romSourceMode='ota',romReference='miui14',romFirmwarePolicy='rom')
        self.data['runtimeBackend']=backend_hint(self.data['runtimePath'])

    @Slot(str)
    def selectDevice(self,serial):
        if not self.configEditable():return
        self.data.update(romPreview={},romPreviewMode=False)
        self.update(selectedSerial=serial,partitionRows=[],inventorySummary={},selectedPartition='',targetKind='',installTargets=[],linuxBundles={},defaultTarget='',
            editActions=[],newRoots=[],newUserdata=0,newUserdataBytes=0,wizardPlan={},wizardStep=0,environment={},compatibility={},firmwareSummary='',firmwareSlots='',firmwareRecordPath='',inspectionOnly=False,
            switchSystems=[],bootSwitchMode=False,bootStoreMode=False,bootStoreReport={},fixedRecoveryPolicy='auto')
        self._auto_inspect=True;self.maybe_inspect()

    def maybe_inspect(self):
        if self._auto_inspect and self.data['workerReady'] and not self.data['busy'] and self.data['selectedSerial']:
            self._auto_inspect=False;self.inspectEnvironment()

    @Slot()
    def inspectEnvironment(self):
        if not self.configEditable() or not self.data['selectedSerial']:return
        self.data['romPreview']={}
        self._auto_inspect=False
        self._environment_before_probe=self.data['environment']
        self.update(error='',environment={},compatibility={},firmwareSummary='',firmwareSlots='',firmwareRecordPath='',message='只读识别当前系统与固件，不重启平板。')
        self.send('inspect_environment',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'])

    @Slot()
    def scanDevice(self):
        if not self.configEditable() or not self.data['selectedSerial']:return
        self.data.update(romPreview={},romPreviewMode=False)
        self._auto_inspect=False
        self.update(wizardStep=0,error='',partitionRows=[],inventorySummary={},selectedPartition='',targetKind='',installTargets=[],linuxBundles={},defaultTarget='',editActions=[],newRoots=[],newUserdata=0,newUserdataBytes=0,
            switchSystems=[],bootSwitchMode=False,bootStoreMode=False,bootStoreReport={})
        self.send('wizard_scan',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'])

    @Slot(str)
    def exportNativeLogs(self,value):
        if not self.configEditable() or not self.data['selectedSerial']:return
        url=QUrl(value);path=url.toLocalFile() if url.isLocalFile() else value
        self.update(error='',nativeLogPath='')
        self.send('native_export_logs',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'],destination=path)

    def rows_changed(self):
        self.data['wizardPlan']={};self.data['romPreview']={};self.changed.emit()

    def selected_targets(self,names):
        self.data['installTargets']=names
        self.data['targetKind']='combined' if len(names)>1 else 'android' if names==['userdata'] else 'linux' if names else ''
        if self.data.get('defaultTarget') not in names:self.data['defaultTarget']=names[0] if names and self.data['setDefault'] else ''

    @Slot(str)
    def selectPartition(self,name):
        if not self.configEditable():return
        self.data.update(romPreview={},romPreviewMode=False)
        row=next((r for r in self.data['partitionRows'] if r['name']==name),None)
        if name and not row and name!='userdata' and not is_root_name(name):return
        if row and row['role']=='recovery':return
        if name=='userdata':kind='android'
        elif is_root_name(name):kind='linux'
        else:kind=''
        if row and row['role'] not in ('android','linux'):kind=''
        self.selected_targets([name] if kind in ('android','linux') else [])
        self.update(selectedPartition=name,targetKind=kind,error='')

    @Slot(str,bool)
    def setInstallTarget(self,name,enabled):
        if not self.configEditable():return
        names=list(self.data['installTargets'])
        row=next((r for r in self.data['partitionRows'] if r['name']==name),None)
        draft=name=='userdata' and self.data['newUserdata'] or any(r['partition_name']==name for r in self.data['newRoots'])
        if (name!='userdata' and not is_root_name(name)) or (not draft and not row):return
        if any(a['name']==name and a['action']=='delete' for a in self.data['editActions']) and not draft:return
        if enabled and name not in names:names.append(name)
        if not enabled and name in names:names.remove(name)
        self.selected_targets(names)
        self.update(selectedPartition=name,error='',romPreview={},romPreviewMode=False,wizardPlan={})

    @Slot(str,str)
    def setLinuxMaterial(self,name,value):
        if not self.configEditable() or name not in self.data['installTargets']:return
        url=QUrl(value);self.data['linuxBundles'][name]=url.toLocalFile() if url.isLocalFile() else value
        self.update(error='',wizardPlan={})

    @Slot(str)
    def setDefaultTarget(self,name):
        if not self.configEditable() or (name and name not in self.data['installTargets']):return
        self.update(defaultTarget=name,setDefault=bool(name),wizardPlan={})

    def partition_configuration(self):
        data = self.data
        config = dict(roots=data['newRoots'], userdata_gib=0, boot_gib=2)
        if data['newUserdataBytes']:
            config['userdata_bytes'] = data['newUserdataBytes']
        return config

    @Slot(str,result='QVariantMap')
    def partitionCapacity(self, kind):
        summary = self.data['inventorySummary']
        if not summary.get('can_create'):
            return dict(max_bytes=0, max_gib=0, reason=summary.get('reason') or '请先读取平板分区。')
        deleting_userdata = any(action['name']=='userdata' and action['action']=='delete'
                                for action in self.data['editActions'])
        if kind == 'android' and (self.data['newUserdataBytes']
                                  or (summary.get('has_userdata') and not deleting_userdata)):
            return dict(max_bytes=0, max_gib=0, reason='已有 userdata，请选中它安装 Android。')
        try:
            maximum = addition_capacity(summary, self.data['partitionRows'], self.data['editActions'],
                                        self.partition_configuration(), kind)
        except ValueError as error:
            return dict(max_bytes=0, max_gib=0, reason=str(error))
        reason = '' if maximum >= 16*GIB else '可用于新分区的连续空间不足 16 GiB。'
        return dict(max_bytes=maximum, max_gib=maximum//GIB, reason=reason)

    def drafts_fit(self, actions):
        if not (self.data['newRoots'] or self.data['newUserdataBytes']):
            return True
        try:
            addition_capacity(self.data['inventorySummary'], self.data['partitionRows'],
                              actions, self.partition_configuration(), None)
        except ValueError as error:
            self.update(error=f'待新建的分区需要重新调整。{error}')
            return False
        return True

    @Slot(str,int,str,str)
    def createPartition(self, kind, size, filesystem, name):
        self.add_partition(kind, size*GIB, filesystem, name)

    @Slot(str,str,str)
    def createPartitionUsingAvailable(self, kind, filesystem, name):
        self.add_partition(kind, None, filesystem, name)

    def add_partition(self, kind, size, filesystem, name):
        if not self.configEditable() or kind not in ('android', 'linux'):
            return
        capacity = self.partitionCapacity(kind)
        if size is None:
            size = capacity['max_bytes']
        if capacity['reason']:
            self.update(error=capacity['reason'])
            return
        if size < 16*GIB:
            self.update(error='系统分区至少需要 16 GiB。')
            return
        if size > capacity['max_bytes']:
            self.update(error=f'容量超过可用连续空间，最多可用 {capacity["max_bytes"]/GIB:.2f} GiB。')
            return
        if kind == 'android':
            self.data.update(newUserdata=size//GIB,newUserdataBytes=size)
            selected = 'userdata'
        else:
            os_id = str(uuid.uuid4())
            selected = root_name(os_id)
            draft = dict(id=os_id,partition_name=selected,name=name.strip() or 'Linux',
                         size_gib=size//GIB,filesystem=filesystem,bundle='',boot_image='')
            if size % GIB:
                draft['size_bytes'] = size
            self.data['newRoots'].append(draft)
        self.selected_targets([selected]);self.update(selectedPartition=selected,targetKind=kind,error='');self.rows_changed()

    @Slot(str,str,str)
    def editPartition(self,name,action,filesystem):
        if not self.configEditable():return
        draft=next((r for r in self.data['newRoots'] if root_name(r['id'])==name),None)
        if draft:
            if action=='delete':self.data['newRoots'].remove(draft)
            else:draft['filesystem']=filesystem
            if action=='delete':self.selected_targets([n for n in self.data['installTargets'] if n!=name])
            self.update(selectedPartition='');self.rows_changed();return
        if name=='userdata' and self.data['newUserdata']:
            if action=='delete':self.data.update(newUserdata=0,newUserdataBytes=0)
            if action=='delete':self.selected_targets([n for n in self.data['installTargets'] if n!=name])
            self.update(selectedPartition='');self.rows_changed();return
        row=next((p for p in self.data['partitionRows'] if p['name']==name),None)
        if not row or not row.get('can_'+action):self.update(error=(row or {}).get('reason','该分区只读。'));return
        actions = [a for a in self.data['editActions'] if a['name']!=name]
        actions.append(dict(name=name,partuuid=row['partuuid'],action=action,filesystem=filesystem))
        if not self.drafts_fit(actions):return
        self.data['editActions'] = actions
        if action=='delete':self.selected_targets([n for n in self.data['installTargets'] if n!=name]);self.data['selectedPartition']=''
        self.update(error='');self.rows_changed()

    @Slot(str)
    def undoEdit(self,name):
        if not self.configEditable():return
        actions = [a for a in self.data['editActions'] if a['name']!=name]
        if not self.drafts_fit(actions):return
        self.update(editActions=actions,error='');self.rows_changed()

    @Slot()
    def nextFromPartitions(self):
        if not self.configEditable() or not self.data['inventorySummary']:return
        if self.data.get('inspectionOnly'):
            if self.data['targetKind']=='android':self.startRomPreview()
            else:self.update(error='此 Recovery 测试版仅开放读取及 Android ROM 安装预览。')
            return
        if not self.data['targetKind'] and not(self.data['editActions'] or self.data['newRoots'] or self.data['newUserdata']):
            self.update(error='请选择 userdata、rootfs，或添加分区操作。');return
        if not self.drafts_fit(self.data['editActions']):return
        self._back_to=1
        self.update(wizardStep=1,error='',bootSwitchMode=False)

    @Slot(str)
    def prepareDefaultSwitch(self, system_id):
        if not self.configEditable():
            return
        data = self.data
        snapshot_id = data['inventorySummary'].get('snapshot_id')
        if not snapshot_id or not data['selectedSerial'] or data['inspectionOnly']:
            return
        if data['editActions'] or data['newRoots'] or data['newUserdata']:
            self.update(error='请先处理待执行的分区操作，再切换默认系统。')
            return
        if not any(row['id']==system_id and not row['is_default'] for row in data['switchSystems']):
            return
        self._back_to=0
        self.update(wizardStep=2,wizardPlan={},error='',romPreviewMode=False,romPreview={},bootSwitchMode=True,bootStoreMode=False)
        self.send('wizard_switch_prepare',serial=data['selectedSerial'],runtime_path=data['runtimePath'],
                  snapshot_id=snapshot_id,system_id=system_id)

    @Slot()
    def inspectBootStore(self):
        if not self.configEditable() or not self.data['selectedSerial']:return
        if self.data['editActions'] or self.data['newRoots'] or self.data['newUserdata']:
            self.update(error='请先撤销或完成待执行的分区操作，再检查引导存储。');return
        self._auto_inspect=False
        self.update(error='',bootStoreReport={},bootStoreMode=False,bootSwitchMode=False,
                    wizardStep=0,wizardPlan={},romPreviewMode=False)
        self.send('boot_store_inspect',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'])

    @Slot(str,str)
    def prepareBootStore(self,operation,transaction):
        if not self.configEditable() or not self.data['selectedSerial']:return
        report=self.data['bootStoreReport']
        if not report.get('available') or self.data['editActions'] or self.data['newRoots'] or self.data['newUserdata']:return
        self._back_to=0
        self.update(error='',wizardStep=2,wizardPlan={},bootStoreMode=True,bootSwitchMode=False,romPreviewMode=False)
        self.send('boot_store_prepare',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'],
                  operation=operation,transaction=transaction,snapshot_id=report['snapshot_id'])

    @Slot(str)
    def preparePartitionResume(self,value):
        if not self.configEditable() or not self.data['selectedSerial']:return
        url=QUrl(value);path=url.toLocalFile() if url.isLocalFile() else value
        self.update(wizardStep=2,wizardPlan={},error='',bootSwitchMode=False)
        self.send('wizard_resume_prepare',serial=self.data['selectedSerial'],runtime_path=self.data['runtimePath'],
            backup_parent=self.data['backupPath'],resume_receipt=path)

    @Slot(str,str)
    def setMaterial(self,kind,value):
        if not self.configEditable():return
        field={'linux':'linuxBundle','android':'romPath'}.get(kind)
        if field:
            url=QUrl(value);self.update(**{field:url.toLocalFile() if url.isLocalFile() else value},error='',romPreview={})

    @Slot()
    def startRomPreview(self):
        if not self.configEditable() or not self.data['selectedSerial']:return
        self.update(romPreviewMode=True,romPreview={},wizardPlan={},targetKind='android',wizardStep=1,error='',bootSwitchMode=False)

    @Slot(str,str)
    def setRomPreviewOption(self,name,value):
        if not self.configEditable():return
        choices={'source':('romSourceMode',('file','reference','ota')),'reference':('romReference',('miui14','hyperos206','miku26101322','lineage241022')),'firmware':('romFirmwarePolicy',('preserve','rom'))}
        if name not in choices or value not in choices[name][1]:return
        if name=='source' and value=='ota':self.data['romFirmwarePolicy']='rom'
        self.update(**{choices[name][0]:value},romPreview={},error='')

    @Slot(bool)
    def setWorkflowDefault(self,value):
        if self.configEditable():self.update(setDefault=value,defaultTarget=(self.data['defaultTarget'] or next(iter(self.data['installTargets']),'')) if value else '')

    @Slot(str)
    def setFixedRecoveryPolicy(self,value):
        if self.configEditable() and value in ('auto','rom'):
            self.update(fixedRecoveryPolicy=value,wizardPlan={},error='')

    @Slot()
    def prepareWorkflow(self):
        if not self.configEditable():return
        data=self.data
        if data['romPreviewMode']:
            if data['romSourceMode'] in ('file','ota') and not data['romPath']:self.update(error='请选择本地 ROM，或切换为仅预览已收录基线。');return
            self.update(wizardStep=2,romPreview={},wizardPlan={},error='')
            self.send('rom_preview',serial=data['selectedSerial'],runtime_path=data['runtimePath'],rom_path=data['romPath'],
                source_mode=data['romSourceMode'],reference_id=data['romReference'],firmware_policy=data['romFirmwarePolicy'])
            return
        self.update(wizardStep=2,wizardPlan={},error='',bootSwitchMode=False)
        self.send('wizard_prepare',serial=data['selectedSerial'],runtime_path=data['runtimePath'],
            snapshot_id=data['inventorySummary']['snapshot_id'],backup_parent=data['backupPath'],
            actions=data['editActions'],partition_config=self.partition_configuration(),
            target=data['selectedPartition'] if data['targetKind'] else '',linux_bundle=data['linuxBundle'],rom_path=data['romPath'],
            install_targets=data['installTargets'],
            linux_bundles=data['linuxBundles'],default_target=data['defaultTarget'],
            recovery_mode='off',
            fixed_recovery_policy=data['fixedRecoveryPolicy'],set_default=data['setDefault'])

    @Slot(str,bool,bool)
    def executeWorkflow(self,serial,accept,experimental):
        if self.data['romPreviewMode'] or self.data['busy'] or self.data['state']!='prepared':return
        self.send('wizard_install',session_id=self.session_id,plan_id=self.data['wizardPlan']['plan_id'],
            confirmation=dict(serial=serial.strip(),accept_operations=accept,allow_experimental=experimental))

    @Slot(int)
    def back(self,step):
        if self.data['busy']:return
        if self.data['state']=='prepared':self._back_to=step;self.cancel()
        else:self.update(wizardStep=step,error='',bootSwitchMode=self.data['bootSwitchMode'] and step!=0,
                         bootStoreMode=self.data['bootStoreMode'] and step!=0)

    @Slot()
    def cancel(self):
        if self.data.get('wizardStep')==0:self._back_to=0
        super().cancel()

    def present_firmware(self,status):
        fw=status.get('firmware',{});state=fw.get('status','');slot=fw.get('active_slot','')
        labels={'matched':'已匹配固件基线','unknown':'固件基线未收录','mismatch':'固件组合不匹配','incomplete':'固件指纹不完整','unavailable':'待进入原生 Recovery 读取固件指纹'}
        rows=[]
        for name,item in fw.get('slots',{}).items():
            text=name.upper()+' 槽：'+labels.get(item.get('status'),item.get('status',''))
            if item.get('label'):text+=' · '+item['label']
            if item.get('status')=='matched' and not item.get('temporary_boot_verified'):text+='（此槽启动尚未验收）'
            rows.append(text)
        self.data.update(firmwareSummary=(('当前 '+slot.upper()+' 槽 · ') if slot else '')+labels.get(state,''),
            firmwareSlots='\n'.join(rows),firmwareRecordPath=status.get('firmware_record_path',''))

    def handle_event(self,event):
        # The base owns worker/log lifecycle; this class owns wizard state.
        previous_serial=self.data['selectedSerial']
        super().handle_event(event)
        kind=event.get('type')
        if kind=='devices' and self.data['selectedSerial']!=previous_serial:
            self.data.update(romPreview={},romPreviewMode=False,switchSystems=[],bootSwitchMode=False,bootStoreMode=False,bootStoreReport={})
            self.data.update(partitionRows=[],inventorySummary={},selectedPartition='',targetKind='',installTargets=[],linuxBundles={},defaultTarget='',editActions=[],newRoots=[],newUserdata=0,newUserdataBytes=0,wizardStep=0,environment={},compatibility={},firmwareSummary='',firmwareSlots='',firmwareRecordPath='')
        if kind=='devices':self._auto_inspect=True
        if kind in ('environment','firmware'):
            self.data['romPreview']={}
            previous=getattr(self,'_environment_before_probe',{})
            if kind=='environment' and previous and previous!=event['environment']:
                self.data.update(partitionRows=[],inventorySummary={},selectedPartition='',targetKind='',installTargets=[],linuxBundles={},defaultTarget='',
                    editActions=[],newRoots=[],newUserdata=0,newUserdataBytes=0,wizardPlan={},switchSystems=[],bootStoreReport={})
            self.data.update(environment=event['environment'],compatibility=event['compatibility'])
            self.present_firmware(event['compatibility'])
            if event.get('runtime_path'):
                self.data['runtimePath']=event['runtime_path']
                self.data['runtimeBackend']=backend_hint(event['runtime_path'])
                if event['environment'].get('properties',{}).get('usb_transport')=='adb' or event['compatibility'].get('usb_transport')=='adb':self.data['runtimeBackend']='adb-native'
        if kind=='native_logs_exported':self.data.update(nativeLogPath=event['path'],message='Recovery 日志已保存。')
        if kind=='rom_preview':self.data.update(romPreview=event['preview'],romPreviewMode=True,wizardStep=2,message='ROM 安装预览已生成，没有写入设备。')
        if kind=='wizard_inventory':
            self.data.update(partitionRows=event['partitions'],inventorySummary=event['summary'],runtimePath=event['runtime_path'],wizardStep=0,
                inspectionOnly=event.get('inspection_only',False),switchSystems=event.get('switch_systems',[]),bootSwitchMode=False)
            self.data['runtimeBackend']=event.get('runtime_backend',backend_hint(event['runtime_path']))
        elif kind=='boot_store_report':self.data.update(bootStoreReport=event['report'],wizardStep=0)
        elif kind=='wizard_plan':self.data.update(wizardPlan=event['plan'],wizardStep=2,
            bootStoreMode=event['plan'].get('kind')=='maintenance')
        elif kind=='wizard_complete':self.data.update(receipt=event['receipt'],wizardStep=4)
        elif kind=='state':
            if event['state']=='installing':self.data['wizardStep']=3
            elif event['state']=='cancelled':
                self.data['wizardStep']=self._back_to
                if self.data['bootSwitchMode'] or self.data['bootStoreMode']:
                    self.data.update(bootSwitchMode=False,bootStoreMode=False,wizardPlan={})
            elif event['state']=='failed' and self.data.get('wizardStep')==3:self.data['wizardStep']=4
        elif kind=='activity' and not event['busy']:
            QTimer.singleShot(0,self.maybe_inspect)
        self.changed.emit()
