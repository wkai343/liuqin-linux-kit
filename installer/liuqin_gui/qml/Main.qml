import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "installerWindow"
    width: 1200; height: 820; minimumWidth: 1040; minimumHeight: 720
    visible: true; color: "#f2f5f7"; title: "Liuqin 安装助手"
    font.family: Qt.platform.os === "windows" ? "Microsoft YaHei UI" : "Noto Sans CJK SC"
    font.pixelSize: 14
    property var v: backend.view
    Component.onCompleted: v=backend.view
    property color ink: "#152d3b"
    property color muted: "#617583"
    property color accent: "#087f83"
    property var steps: ["设备与分区", "安装材料", "确认方案", "执行", "完成"]
    property var selectedRow: rows().find(function(row) { return row.name === v.selectedPartition && row.draft }) || rows().find(function(row) { return row.name === v.selectedPartition }) || ({})
    property string lastPlan: ""
    property string linuxDialogTarget: ""
    property bool hasPartitionEdits: (v.editActions||[]).length>0 || (v.newRoots||[]).length>0 || !!v.newUserdata
    property var installRows: (v.installTargets||[]).map(function(name) { return window.rows().find(function(r){return r.name===name&&r.draft}) || window.rows().find(function(r){return r.name===name}) || {name:name,display_name:name} })
    property bool installAndroid: (v.installTargets||[]).indexOf("userdata")>=0
    property var defaultChoices: [{name:"",label:"保留当前默认引导"}].concat(installRows.map(function(r){return {name:r.name,label:r.name==="userdata" ? "Android" : r.display_name||r.name} }))
    function gib(bytes) { return (Number(bytes || 0) / 1073741824).toFixed(2) + " GiB" }
    function canCleanupBootStore() {
        const report = v.bootStoreReport || {}
        if (!report.available || report.pending_count !== 0 || !report.default_matches || report.retention_error) return false
        if (report.cleanup_available !== undefined) return report.cleanup_available === true
        // Retention-capable images shipped before the explicit availability flag.
        if (report.retention) return (report.retention.units || []).length > 0
        return (report.duplicates || []).length > 0 || report.reclaimable_bytes > 0
    }
    function operation(name) { return (v.editActions || []).find(function(a) { return a.name === name }) || ({}) }
    function rows() {
        let result = (v.partitionRows || []).slice()
        for (const r of (v.newRoots || [])) result.push({name:r.partition_name, display_name:r.name, bytes:r.size_bytes || r.size_gib*1073741824, filesystem:r.filesystem, role:"linux", can_delete:true, can_format:true, can_install:true, draft:true})
        if (v.newUserdata) result.push({name:"userdata",display_name:"userdata",bytes:v.newUserdataBytes || v.newUserdata*1073741824,filesystem:"F2FS",role:"android",can_delete:true,can_format:true,can_install:true,draft:true})
        return result
    }
    onClosing: function(close) { close.accepted=backend.closeWindow() }
    Connections {
        target: backend
        function onChanged() {
            window.v=backend.view
            if (newPartition.visible)
                newPartition.refreshCapacity()
            if (window.v.wizardPlan.plan_id && window.v.wizardPlan.plan_id !== window.lastPlan) {
                window.lastPlan=window.v.wizardPlan.plan_id; consent.checked=false
            }
        }
    }
    component Action: Button {
        property bool primary:false
        implicitHeight:42; leftPadding:18; rightPadding:18
        background: Rectangle { radius:7; color:!parent.enabled ? "#e8edf0" : parent.primary ? window.accent : "white"; border.color:parent.primary ? "transparent" : "#ccd9e0" }
        contentItem: Text { text:parent.text; color:!parent.enabled ? "#90a0aa" : parent.primary ? "white" : window.ink; horizontalAlignment:Text.AlignHCenter; verticalAlignment:Text.AlignVCenter }
    }
    component Card: Rectangle {
        default property alias contents: column.data
        implicitHeight:column.implicitHeight+36; color:"white"; border.color:"#dde6eb"; radius:11
        ColumnLayout { id:column; anchors { left:parent.left;right:parent.right;top:parent.top;margins:18 }spacing:12 }
    }
    component Body: Text { Layout.fillWidth:true; wrapMode:Text.WordWrap; color:window.muted; lineHeight:1.3 }
    component Heading: Text { color:window.ink; font.pixelSize:18; font.weight:Font.DemiBold }
    component Page: ScrollView {
        id: pageScroll
        default property alias contents: column.data
        contentWidth:availableWidth; clip:true
        ColumnLayout { id:column;width:pageScroll.availableWidth;spacing:16 }
    }
    FolderDialog { id:linuxDialog;title:"选择 Linux boot.img 和 rootfs.tar.gz 所在目录";onAccepted:{if(window.linuxDialogTarget)backend.setLinuxMaterial(window.linuxDialogTarget,selectedFolder.toString());else backend.setMaterial("linux",selectedFolder.toString())} }
    FolderDialog { id:romFolder;title:"选择 Android 镜像目录";onAccepted:backend.setMaterial("android",selectedFolder.toString()) }
    FileDialog { id:romFile;title:"选择完整 Android ROM";nameFilters:["Android ROM (*.zip *.tgz *.tar.gz)"];onAccepted:backend.setMaterial("android",selectedFile.toString()) }
    FolderDialog { id:backupDialog;title:"选择备份位置";onAccepted:backend.setBackupPath(selectedFolder.toString()) }
    FileDialog { id:resumeDialog;title:"选择分区中断的 receipt.json";nameFilters:["执行记录 (receipt.json)"];onAccepted:backend.preparePartitionResume(selectedFile.toString()) }
    FolderDialog { id:reportDialog;title:"保存报告";onAccepted:backend.exportReport(selectedFolder.toString()) }
    FolderDialog { id:nativeLogDialog;title:"保存 Recovery 日志";onAccepted:backend.exportNativeLogs(selectedFolder.toString()) }
    Dialog {
        id: newPartition
        objectName: "newPartitionDialog"
        title: "新建系统分区"
        modal: true
        anchors.centerIn: parent
        width: 450
        standardButtons: Dialog.Ok | Dialog.Cancel

        readonly property string kind: newKind.currentIndex === 0 ? "linux" : "android"
        property var capacity: ({max_bytes: 0, max_gib: 0, reason: ""})
        readonly property bool hasSpace: Number(capacity.max_bytes) >= 16 * 1073741824
        readonly property bool canCreate: hasSpace && !window.v.busy
            && (useAll || (newSize.contentItem && newSize.contentItem.acceptableInput
                && newSize.value <= capacity.max_gib))
        property bool useAll: false

        function refreshCapacity() {
            capacity = backend.partitionCapacity(kind)
        }
        onKindChanged: {
            useAll = false
            if (visible)
                refreshCapacity()
        }
        onOpened: {
            useAll = false
            refreshCapacity()
            newSize.value = Math.min(32, newSize.to)
        }
        Component.onCompleted: {
            const ok = standardButton(Dialog.Ok)
            ok.objectName = "newPartitionOk"
            ok.enabled = Qt.binding(() => newPartition.canCreate)
        }

        ColumnLayout {
            width:parent.width;spacing:12
            ComboBox { id:newKind;objectName:"newPartitionKind";Layout.fillWidth:true;model:["Linux rootfs","Android userdata"] }
            TextField { id:newName;Layout.fillWidth:true;placeholderText:"名称，例如 Fedora";visible:newPartition.kind==="linux" }
            Body {
                text: newPartition.hasSpace
                    ? "最大可用连续空间 " + window.gib(newPartition.capacity.max_bytes)
                    : (newPartition.capacity.reason || "可用连续空间不足 16 GiB。")
                color: newPartition.hasSpace ? window.muted : "#94481e"
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: newPartition.useAll ? "容量" : "容量（GiB）" }
                SpinBox {
                    id: newSize
                    objectName: "newPartitionSize"
                    from: 16
                    to: Math.max(from, Number(newPartition.capacity.max_gib) || 0)
                    value: 32
                    editable: true
                    enabled: newPartition.hasSpace && !newPartition.useAll
                    visible: !newPartition.useAll
                }
                Text {
                    visible: newPartition.useAll
                    text: window.gib(newPartition.capacity.max_bytes)
                }
                Item { Layout.fillWidth: true }
                Action {
                    objectName: "newPartitionUseAll"
                    text: "使用所有未分配空间"
                    checkable: true
                    checked: newPartition.useAll
                    primary: checked
                    enabled: newPartition.hasSpace
                    onClicked: newPartition.useAll = checked
                }
            }
            Body {
                visible: newPartition.useAll
                text: "使用扣除待新建分区和所需引导空间后，最大的一段连续空间。"
            }
            ComboBox { id:newFs;model:["ext4","btrfs"];visible:newPartition.kind==="linux" }
            Body { text:"操作先加入列表，确认方案前不会改写平板。删除操作所释放的空间可用于本次分配。" }
        }
        onAccepted: {
            if (!canCreate) return
            if (useAll)
                backend.createPartitionUsingAvailable(kind, newFs.currentText, newName.text)
            else
                backend.createPartition(kind, newSize.value, newFs.currentText, newName.text)
        }
    }
    RowLayout {
        anchors.fill:parent;spacing:0
        Rectangle {
            Layout.preferredWidth:220;Layout.fillHeight:true;color:"#122b38"
            ColumnLayout {
                anchors.fill:parent;anchors.margins:24;spacing:12
                Text { text:"LIUQIN";color:"#95dfd8";font.letterSpacing:3;Layout.topMargin:18 }
                Text { text:"安装助手";color:"white";font.pixelSize:26;font.weight:Font.DemiBold }
                Text { text:"小米平板 6 Pro";color:"#9ab3c0";Layout.bottomMargin:30 }
                Repeater {
                    model:window.steps
                    delegate:Rectangle {
                        required property int index;required property string modelData
                        Layout.fillWidth:true;height:52;radius:8;color:index===window.v.wizardStep ? "#24505b" : "transparent"
                        Text { anchors.verticalCenter:parent.verticalCenter;anchors.left:parent.left;anchors.leftMargin:12;text:(index+1)+"   "+modelData;color:index===window.v.wizardStep ? "white" : "#9ab3c0" }
                    }
                }
                Item { Layout.fillHeight:true }
                Body { text:window.v.simulation ? "演示模式 · 不连接设备" : "先选分区，再选择系统材料";color:"#9ab3c0" }
            }
        }
        ColumnLayout {
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.margins:28;spacing:16
            Text { text:window.steps[window.v.wizardStep];font.pixelSize:28;font.weight:Font.DemiBold;color:window.ink }
            Rectangle { visible:!!window.v.error;Layout.fillWidth:true;implicitHeight:err.implicitHeight+24;radius:8;color:"#fff0e7";Text { id:err;anchors.fill:parent;anchors.margins:12;text:window.v.error;wrapMode:Text.WrapAnywhere;color:"#94481e" } }
            StackLayout {
                Layout.fillWidth:true;Layout.fillHeight:true;currentIndex:window.v.wizardStep
                Page {
                    objectName:"partitionPage"
                    Card {
                        Layout.fillWidth:true
                        RowLayout {
                            Heading { text:"连接平板";Layout.fillWidth:true }
                            Action { text:"刷新设备";enabled:!window.v.busy;onClicked:backend.probeDevices() }
                            Action { id:toolsToggle;objectName:"advancedToolsToggle";checkable:true;text:checked ? "收起设置与诊断 ▴" : "设置与诊断 ▾" }
                        }
                        RowLayout {
                            ComboBox { objectName:"deviceChoice";Layout.fillWidth:true;model:window.v.devices;textRole:"serial";currentIndex:model.findIndex(function(d){return d.serial===window.v.selectedSerial});enabled:!window.v.busy;onActivated:backend.selectDevice(model[currentIndex].serial) }
                            Action { objectName:"readDeviceButton";text:window.v.inventorySummary.snapshot_id ? "重新读取分区" : "读取分区";primary:true;enabled:!!window.v.selectedSerial&&!window.v.busy&&window.v.compatibility.ready===true;onClicked:backend.scanDevice() }
                        }
                        Body { visible:!window.v.selectedSerial;text:"请连接平板。使用 Android 时，请开启 USB 调试并授权这台电脑。" }
                        RowLayout {
                            visible:!!window.v.environment.label
                            Body { objectName:"environmentLabel";text:window.v.environment.label||"" }
                            Text {
                                visible:window.v.compatibility.ready===true&&!window.v.busy
                                text:window.v.inventorySummary.snapshot_id ? "分区已读取" : "可读取分区"
                                color:window.accent
                            }
                        }
                        Body { objectName:"compatibilityLabel";visible:!!window.v.compatibility.reason&&window.v.compatibility.ready!==true;text:window.v.compatibility.reason||"";color:"#94481e" }
                        Body { visible:window.v.compatibility.ready===true&&window.v.compatibility.native===true&&window.v.environment.kind!=="native-recovery"&&!window.v.busy;text:"读取分区时会经 Bootloader Fastboot 临时启动 Recovery。" }
                        Body { visible:!window.v.runtimePath&&!window.v.simulation;text:"未找到配套 Recovery。请使用内置镜像的安装包，或用 --recovery-image 指定镜像。" }
                        ColumnLayout {
                            objectName:"advancedTools"
                            visible:toolsToggle.checked
                            Layout.fillWidth:true
                            spacing:10
                            Rectangle { Layout.fillWidth:true;implicitHeight:1;color:"#e0e7ec" }
                            Body { text:"运行环境";font.weight:Font.DemiBold;color:window.ink }
                            Body { visible:window.v.compatibility.ready===true&&!!window.v.compatibility.reason;text:window.v.compatibility.reason||"" }
                            Action { objectName:"inspectEnvironmentButton";text:"识别系统与固件";enabled:!!window.v.selectedSerial&&!window.v.busy;onClicked:backend.inspectEnvironment() }
                            Body { objectName:"firmwareSummaryLabel";visible:!!window.v.firmwareSummary;text:window.v.firmwareSummary||"" }
                            Body { objectName:"firmwareSlotsLabel";visible:!!window.v.firmwareSlots;text:window.v.firmwareSlots||"" }
                            Body { objectName:"fixedCandidateLabel";visible:!!window.v.compatibility.fixed_candidate;text:window.v.compatibility.fixed_candidate ? window.v.compatibility.fixed_candidate.reason : "" }
                            Body { objectName:"universalCandidateLabel";visible:!!window.v.compatibility.universal_candidate;text:window.v.compatibility.universal_candidate ? window.v.compatibility.universal_candidate.reason : "";color:"#94481e" }
                            Body { objectName:"firmwareRecordLabel";visible:!!window.v.firmwareRecordPath;text:"固件指纹记录 · "+(window.v.firmwareRecordPath||"");wrapMode:Text.WrapAnywhere }
                            Body { text:"维护与检查";font.weight:Font.DemiBold;color:window.ink }
                            GridLayout {
                                columns:2;uniformCellWidths:true;Layout.fillWidth:true
                                Action { objectName:"romPreviewEntry";text:"预览 ROM 刷写计划";Layout.fillWidth:true;enabled:!!window.v.selectedSerial&&!window.v.busy;onClicked:backend.startRomPreview() }
                                Action { objectName:"exportNativeLogs";text:"导出 Recovery 日志";Layout.fillWidth:true;visible:window.v.compatibility.native===true;enabled:!!window.v.selectedSerial&&!window.v.busy;onClicked:nativeLogDialog.open() }
                                Action { objectName:"bootStoreEntry";text:"引导存储与中断恢复";Layout.fillWidth:true;enabled:!!window.v.selectedSerial&&!window.v.busy;onClicked:backend.inspectBootStore() }
                            }
                        }
                        Body { visible:!!window.v.nativeLogPath;text:"日志已保存至 "+(window.v.nativeLogPath||"");wrapMode:Text.WrapAnywhere }
                        ProgressBar { visible:window.v.busy;Layout.fillWidth:true;indeterminate:window.v.progress<0;value:Math.max(0,window.v.progress) }
                        Body { visible:window.v.busy;text:window.v.message }
                    }
                    Card {
                        objectName:"bootStoreCard"
                        Layout.fillWidth:true
                        visible:Object.keys(window.v.bootStoreReport||{}).length>0
                        Heading { text:"引导存储与中断恢复" }
                        Body { visible:!window.v.bootStoreReport.available;text:window.v.bootStoreReport.message||"" }
                        Body {
                            visible:!!window.v.bootStoreReport.available
                            text:"可用 "+window.gib(window.v.bootStoreReport.free_bytes)+" / "+window.gib(window.v.bootStoreReport.total_bytes)
                                +" · 文件实际占用 "+window.gib(window.v.bootStoreReport.allocated_bytes)
                                +" · 可回收过期版本 "+window.gib(window.v.bootStoreReport.reclaimable_bytes)
                        }
                        Body {
                            visible:!!window.v.bootStoreReport.available
                            text:"默认系统："+(window.v.bootStoreReport.default_name||"未登记")
                                +(window.v.bootStoreReport.default_matches ? " · 引导匹配" : " · 引导与登记不匹配，请先检查中断记录")
                            color:window.v.bootStoreReport.default_matches ? window.muted : "#94481e"
                        }
                        RowLayout {
                            Action { text:"刷新检查";enabled:!window.v.busy;onClicked:backend.inspectBootStore() }
                            Action {
                                objectName:"prepareStoreCleanup";text:window.v.bootStoreReport.cleanup_pending ? "继续清理" : "预览空间回收"
                                visible:!!window.v.bootStoreReport.available
                                enabled:!window.v.busy&&!window.hasPartitionEdits&&window.canCleanupBootStore()
                                onClicked:backend.prepareBootStore("cleanup","")
                            }
                            CheckBox { id:showTransactions;text:"显示历史记录";visible:!!window.v.bootStoreReport.available }
                        }
                        Body { visible:window.v.bootStoreReport.pending_count>0;text:"有 "+String(window.v.bootStoreReport.pending_count||0)+" 个未结束记录。只有恢复材料和当前设备匹配时才提供操作。";color:"#94481e" }
                        Body { visible:!!window.v.bootStoreReport.cleanup_pending;text:"上次历史清理尚未结束，请先继续清理，再安装或切换系统。";color:"#94481e" }
                        Body { visible:!!window.v.bootStoreReport.retention_error;text:window.v.bootStoreReport.retention_error||"";color:"#94481e" }
                        Repeater {
                            model:(window.v.bootStoreReport.transactions||[]).filter(function(r){return showTransactions.checked||r.status==="pending"})
                            delegate:ColumnLayout {
                                id:transactionRow
                                required property var modelData
                                Layout.fillWidth:true
                                Body { text:modelData.name+" · "+({complete:"已完成","rolled-back":"已回退",record:"分区记录",superseded:"已由后续安装替代",pending:"未结束"}[modelData.status]||modelData.status);wrapMode:Text.WrapAnywhere }
                                Body { visible:!!modelData.message;text:modelData.message||"" }
                                Repeater {
                                    model:modelData.actions||[]
                                    delegate:Action {
                                        required property var modelData
                                        objectName:"storeAction_"+modelData.operation
                                        property string transactionName:transactionRow.modelData.name
                                        text:modelData.label;enabled:!window.v.busy&&!window.hasPartitionEdits
                                        onClicked:backend.prepareBootStore(modelData.operation,transactionName)
                                    }
                                }
                            }
                        }
                    }
                    Card {
                        objectName: "defaultSwitchCard"
                        Layout.fillWidth: true
                        visible: !!window.v.inventorySummary.snapshot_id && window.v.switchSystems.length > 0
                        Heading { text: "默认启动系统" }
                        Body {
                            visible: window.hasPartitionEdits
                            text: "请先处理待执行的分区操作，再切换默认系统。"
                        }
                        Repeater {
                            model: window.v.switchSystems
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                Body { text: modelData.name + " · " + modelData.partition }
                                Text {
                                    visible: modelData.is_default
                                    text: "当前默认"
                                    color: window.accent
                                }
                                Action {
                                    objectName: "switchDefault_" + modelData.id
                                    visible: !modelData.is_default
                                    enabled: !window.v.busy && !window.v.inspectionOnly && !window.hasPartitionEdits
                                    text: "设为默认"
                                    onClicked: backend.prepareDefaultSwitch(modelData.id)
                                }
                            }
                        }
                    }
                    Card {
                        Layout.fillWidth:true;visible:!!window.v.inventorySummary.snapshot_id
                        RowLayout { Heading { text:"安装目标";Layout.fillWidth:true }CheckBox { id:allPartitions;objectName:"allPartitionsToggle";text:"显示全部分区" } }
                        Body { text:"勾选 userdata 安装 Android，勾选 rootfs 安装 Linux，可同时选择。" }
                        Body { visible:window.v.inspectionOnly;text:"当前 Recovery 仅供读取分区和预览 Android ROM。";color:"#94481e" }
                        RowLayout {
                            Body { text:"当前未分配空间  "+window.gib(window.v.inventorySummary.free_bytes) }
                            Action { objectName:"newPartitionButton";text:"新建分区";visible:!window.v.inspectionOnly;enabled:!!window.v.inventorySummary.can_create&&!window.v.busy;onClicked:newPartition.open() }
                        }
                        Body { visible:partitionRows.count===0;text:"还没有可选的安装目标。可从未分配空间新建系统分区。" }
                        Repeater {
                            id:partitionRows;objectName:"partitionRows"
                            model:window.rows().filter(function(r){return allPartitions.checked||r.role==="android"||r.role==="linux"})
                            delegate:Rectangle {
                                required property var modelData
                                property bool pendingDelete: !modelData.draft && window.operation(modelData.name).action==="delete"
                                Layout.fillWidth:true;implicitHeight:56;radius:7
                                color:(window.v.installTargets||[]).indexOf(modelData.name)>=0&&!pendingDelete ? "#e3f3f0" : modelData.draft ? "#f1f7fd" : "#f7f9fa"
                                border.color:window.v.selectedPartition===modelData.name&&!pendingDelete ? window.accent : "#e0e7ec"
                                RowLayout {
                                    anchors.fill:parent;anchors.leftMargin:12;anchors.rightMargin:12;spacing:12
                                    CheckBox { objectName:"installTarget_"+modelData.name;Layout.preferredWidth:32;visible:modelData.role==="android"||modelData.role==="linux";checked:!pendingDelete&&(window.v.installTargets||[]).indexOf(modelData.name)>=0;enabled:!window.v.busy&&!pendingDelete&&(modelData.can_install||window.v.inspectionOnly&&modelData.role==="android");onClicked:backend.setInstallTarget(modelData.name,checked) }
                                    RadioButton { Layout.preferredWidth:32;visible:modelData.role!=="android"&&modelData.role!=="linux"&&modelData.role!=="recovery";checked:!pendingDelete&&window.v.selectedPartition===modelData.name;enabled:!window.v.busy&&!pendingDelete;onClicked:backend.selectPartition(modelData.name) }
                                    Item { Layout.preferredWidth:32;visible:modelData.role==="recovery" }
                                    ColumnLayout {
                                        Layout.fillWidth:true;spacing:2
                                        Text { Layout.fillWidth:true;elide:Text.ElideRight;text:modelData.display_name||modelData.name;color:window.ink;font.weight:Font.DemiBold }
                                        Text {
                                            Layout.fillWidth:true;elide:Text.ElideRight;color:window.muted;font.pixelSize:12
                                            text:(modelData.role==="android" ? "Android" : modelData.role==="linux" ? modelData.name : modelData.role==="store" ? "引导存储" : modelData.role==="recovery" ? "Recovery" : "系统分区")+(modelData.filesystem ? " · "+modelData.filesystem : "")
                                        }
                                    }
                                    Text { Layout.preferredWidth:90;horizontalAlignment:Text.AlignRight;text:window.gib(modelData.bytes);color:window.ink }
                                    Text { Layout.preferredWidth:72;horizontalAlignment:Text.AlignRight;text:modelData.draft ? "新建" : window.operation(modelData.name).action==="delete" ? "待删除" : window.operation(modelData.name).action==="format" ? "待格式化" : modelData.status==="installed" ? "已安装" : modelData.role==="protected"||modelData.role==="store" ? "受保护" : "已有分区";color:window.operation(modelData.name).action ? "#a34b24" : window.muted }
                                }
                            }
                        }
                        RowLayout {
                            visible:!!window.selectedRow.can_format||!!window.selectedRow.can_delete
                            Body { text:"操作分区  "+(window.selectedRow.display_name||window.selectedRow.name||"") }
                            ComboBox { id:formatFs;model:["ext4","btrfs"];visible:window.selectedRow.role==="linux"&&!!window.selectedRow.can_format }
                            Action { objectName:"formatPartitionButton";text:"格式化";visible:!!window.selectedRow.can_format;enabled:!window.v.busy;onClicked:backend.editPartition(window.v.selectedPartition,"format",formatFs.currentText) }
                            Action { objectName:"deletePartitionButton";text:"删除";visible:!!window.selectedRow.can_delete;enabled:!window.v.busy;onClicked:backend.editPartition(window.v.selectedPartition,"delete","") }
                        }
                        Body { visible:window.v.selectedPartition&&!window.v.targetKind;text:window.selectedRow.reason||"请选择系统分区。" }
                        Repeater {
                            model:window.v.editActions
                            delegate:RowLayout { required property var modelData;Layout.fillWidth:true;Body { text:modelData.name+"："+(modelData.action==="delete" ? "删除分区" : "格式化（清除数据）");color:"#a34b24" }Action { text:"撤销";enabled:!window.v.busy;onClicked:backend.undoEdit(modelData.name) } }
                        }
                        Body { text:window.v.inventorySummary.reason||"" }
                    }
                }
                Page {
                    Card {
                        Layout.fillWidth:true
                        Heading { text:window.v.romPreviewMode ? "分析 Android ROM" : window.v.targetKind==="combined" ? "安装所选系统" : window.v.targetKind==="android" ? "安装 Android" : window.v.targetKind==="linux" ? "安装 Linux" : "分区操作" }
                        Body { text:window.v.romPreviewMode ? "可分析 MIUI、HyperOS、MikuUI、LineageOS 及其他 liuqin 完整 OTA；未准入 ROM 只展示检查结果。只读分析材料，重新读取平板当前固件，推演安装后的 Rec 条件；不会启动刷写或重启。需要原生 Recovery 已运行并连接 USB。" : window.v.targetKind==="combined" ? "Android 和 Linux 均为可选；分别配置本次勾选的安装目标。完整 ROM 会更新共享固件，下方列出其影响。" : window.v.targetKind==="android" ? "选择完整 ROM ZIP。安装会更新包内系统、启动、固件和 Recovery，并清空 Android 数据。Linux rootfs 和已有引导文件保留。" : window.v.targetKind==="linux" ? "目标："+window.v.selectedPartition+"。只需该发行版的 boot.img 和 rootfs.tar.gz，安装会清空所选 rootfs。" : "本次没有系统安装材料要求；分区更改将在确认完整方案后执行。" }
                        ComboBox { objectName:"romSourceMode";visible:window.v.romPreviewMode;Layout.fillWidth:true;model:["分析本地 ROM 目录或完整压缩包","仅预览已收录基线（没有安装材料）","完整 ROM：导入并生成刷写计划"];currentIndex:["file","reference","ota"].indexOf(window.v.romSourceMode);onActivated:backend.setRomPreviewOption("source",["file","reference","ota"][currentIndex]) }
                        ComboBox { objectName:"romReferenceChoice";visible:window.v.romPreviewMode&&window.v.romSourceMode==="reference";Layout.fillWidth:true;model:["本机 MIUI 14.0.3 / Android 13","HyperOS 2.0.6 / Android 15","MikuUI Blooming 26101322 / Android 16","LineageOS 24.0 · 20261022（未准入）"];currentIndex:["miui14","hyperos206","miku26101322","lineage241022"].indexOf(window.v.romReference);onActivated:backend.setRomPreviewOption("reference",["miui14","hyperos206","miku26101322","lineage241022"][currentIndex]) }
                        Body { visible:window.v.targetKind==="combined";text:"为每个所选系统分别提供材料。只安装勾选的目标，全部安装后再设置默认引导。" }
                        Body { visible:window.installAndroid&&!window.v.romPreviewMode;objectName:"androidWriteScope";text:"Android 安装会清空 userdata、metadata，按包更新 super、启动资源和固件。下方可选择自动保护固定 Rec；具体范围会在确认页列出。共享固件更新可能影响 Linux 的后续启动。";color:"#94481e" }
                        RowLayout { visible:window.v.targetKind==="linux";TextField { objectName:"linuxBundlePath";Layout.fillWidth:true;text:window.v.linuxBundles[window.v.installTargets[0]]||window.v.linuxBundle;placeholderText:"Linux 安装包目录";onEditingFinished:backend.setLinuxMaterial(window.v.installTargets[0],text) }Action { text:"选择目录";onClicked:{window.linuxDialogTarget=window.v.installTargets[0];linuxDialog.open()} } }
                        Repeater {
                            model:window.v.targetKind==="combined" ? window.installRows.filter(function(r){return r.name!=="userdata"}) : []
                            delegate:ColumnLayout {
                                required property var modelData;Layout.fillWidth:true
                                Body { text:(modelData.display_name||modelData.name)+" → "+modelData.name+"（将清空此 rootfs）" }
                                RowLayout { TextField { objectName:"linuxBundle_"+modelData.name;Layout.fillWidth:true;text:window.v.linuxBundles[modelData.name]||window.v.linuxBundle;placeholderText:"包含 boot.img 和 rootfs.tar.gz 的目录";onEditingFinished:backend.setLinuxMaterial(modelData.name,text) }Action { text:"选择目录";onClicked:{window.linuxDialogTarget=modelData.name;linuxDialog.open()} } }
                            }
                        }
                        Body { visible:window.installAndroid&&window.v.targetKind==="combined";text:"Android ROM → userdata" }
                        RowLayout { visible:(window.installAndroid||window.v.romPreviewMode)&&(!window.v.romPreviewMode||window.v.romSourceMode!=="reference");TextField { objectName:"androidRomPath";Layout.fillWidth:true;text:window.v.romPath;placeholderText:"完整 ROM 目录或压缩包";onEditingFinished:backend.setMaterial("android",text) }Action { text:"目录";onClicked:romFolder.open() }Action { text:"压缩包";onClicked:romFile.open() } }
                        ComboBox { objectName:"fixedRecoveryPolicyChoice";visible:window.installAndroid&&!window.v.romPreviewMode;Layout.fillWidth:true;model:["自动保留或恢复本机固定 Rec（推荐）","按 ROM 原样处理 Recovery"];currentIndex:window.v.fixedRecoveryPolicy==="rom" ? 1 : 0;onActivated:backend.setFixedRecoveryPolicy(currentIndex===1 ? "rom" : "auto") }
                        Body { visible:window.installAndroid&&!window.v.romPreviewMode;text:window.v.fixedRecoveryPolicy==="rom" ? "允许完整包写入自带 Recovery，固定 Rec 可能被覆盖。" : "检测本机已验收固定 Rec，核对安装后的 ABL、AVB 和内嵌设备树条件。兼容时跳过包内 Recovery；已被覆盖时备份后恢复。没有本机固定 Rec 记录时按 ROM 安装。";color:window.v.fixedRecoveryPolicy==="rom" ? "#94481e" : "#607689" }
                        ComboBox { objectName:"romFirmwarePolicy";visible:window.v.romPreviewMode&&window.v.romSourceMode!=="ota";Layout.fillWidth:true;model:["保留当前底层固件（默认）","采用 ROM 所带底层固件（仅推演，未开放写入）"];currentIndex:window.v.romFirmwarePolicy==="rom" ? 1 : 0;onActivated:backend.setRomPreviewOption("firmware",currentIndex===1 ? "rom" : "preserve") }
                        Body { visible:window.v.romPreviewMode;text:"此处按 ROM 原样推演 A 槽启动资源和包内 Recovery，userdata 与 metadata 列为清空对象。实际安装可选择自动保护固定 Rec，最终范围以安装确认页为准。" }
                        CheckBox { objectName:"defaultBootChoice";visible:!window.v.romPreviewMode&&(window.v.targetKind==="android"||window.v.targetKind==="linux");text:window.v.targetKind==="android" ? "刷入配套 Android 引导并设为默认系统" : "将本次安装的系统设为默认启动";checked:window.v.setDefault;onToggled:backend.setWorkflowDefault(checked) }
                        Body { visible:window.v.targetKind==="combined";text:"安装完成后的默认启动系统" }
                        ComboBox { objectName:"combinedDefaultChoice";visible:window.v.targetKind==="combined";Layout.fillWidth:true;model:window.defaultChoices;textRole:"label";currentIndex:window.defaultChoices.findIndex(function(r){return r.name===window.v.defaultTarget});onActivated:backend.setDefaultTarget(window.defaultChoices[currentIndex].name) }
                    }
                    Card { visible:!window.v.romPreviewMode;Layout.fillWidth:true;Heading { text:"备份与检查" }Body { text:"自动复用同机、同布局且校准数据匹配的备份。每次读取当前引导快照，完整 ROM 的相同分区镜像按校验复用。个人文件需另行备份。" }RowLayout { TextField { Layout.fillWidth:true;text:window.v.backupPath;readOnly:true }Action { text:"更改位置";onClicked:backupDialog.open() } } }
                }
                Page {
                    Card {
                        visible:window.v.romPreviewMode;Layout.fillWidth:true
                        Heading { text:window.v.busy ? "正在只读分析 ROM" : "安装预览（不执行）" }
                        Body { text:window.v.message;visible:window.v.busy }
                        ProgressBar { visible:window.v.busy;Layout.fillWidth:true;indeterminate:true }
                        Body { visible:window.v.romSourceMode==="ota";text:"按完整 ROM 的内容准备刷写。固件、DTBO 版本不同会列入更新计划；Linux rootfs 和已有引导文件保留。" }
                        Body { objectName:"romPreviewSummary";text:window.v.romPreview.rom ? window.v.romPreview.rom.label+" · "+window.v.romPreview.material_status : "选择材料后生成预览。" }
                        Body { text:window.v.romPreview.source ? window.v.romPreview.source.verification : "" }
                        Body { text:window.v.romPreview.abl_source||"" }
                        Body { objectName:"romPreviewBefore";text:window.v.romPreview.rec_before ? "安装前 A 槽："+(window.v.romPreview.rec_before.baseline_label||"ABL 未识别")+" · AVB flags="+String(window.v.romPreview.rec_before.root_avb_flags===undefined ? "未知" : window.v.romPreview.rec_before.root_avb_flags) : "" }
                        Body { objectName:"romPreviewAfter";text:window.v.romPreview.rec_after ? "安装后 A 槽："+(window.v.romPreview.rec_after.baseline_label||"ABL 未识别")+" · AVB flags="+String(window.v.romPreview.rec_after.root_avb_flags===undefined ? "未知" : window.v.romPreview.rec_after.root_avb_flags) : "" }
                        Body { objectName:"romPreviewRecCondition";text:window.v.romPreview.rec_after ? window.v.romPreview.rec_after.basis+"。"+window.v.romPreview.rec_after.reason : "";color:"#94481e" }
                        Repeater { model:window.v.romPreview.assumptions||[];delegate:Body { required property string modelData;text:"基线假设："+modelData;color:"#94481e" } }
                        Heading { visible:!!window.v.romPreview.preview_id;text:"拟议修改与保留范围" }
                        Repeater { model:window.v.romPreview.writes||[];delegate:Body { required property var modelData;text:modelData.partition+"："+modelData.action+"（"+modelData.material+"）" } }
                        Body { visible:!!window.v.romPreview.preview_id;text:"拟清空："+(window.v.romPreview.formats||[]).join("、");color:"#94481e" }
                        Body { visible:!!window.v.romPreview.preview_id;text:"保留："+(window.v.romPreview.preserved||[]).join("、") }
                        Repeater { model:window.v.romPreview.firmware||[];delegate:Body { required property var modelData;text:modelData.partition+"："+modelData.action+"（"+modelData.material+"）";color:modelData.matched===false ? "#94481e" : "#557481" } }
                        Repeater { model:window.v.romPreview.warnings||[];delegate:Body { required property string modelData;text:modelData } }
                        Heading { visible:!!window.v.romPreview.preview_id;text:"尚未满足的条件" }
                        Repeater { model:window.v.romPreview.blockers||[];delegate:Body { required property string modelData;text:modelData;color:"#94481e" } }
                        Body { text:window.v.romPreview.record_path ? "预览记录："+window.v.romPreview.record_path : "" }
                    }
                    Card {
                        visible:!window.v.romPreviewMode
                        Layout.fillWidth:true
                        Heading {
                            text: window.v.bootStoreMode ? (window.v.busy ? "正在核验恢复材料" : "确认引导存储操作") : window.v.bootSwitchMode
                                ? (window.v.busy ? "正在核验目标引导" : "确认切换默认系统")
                                : (window.v.busy ? "正在备份和校验" : "确认完整方案")
                        }
                        Body { visible:!!window.v.wizardPlan.resume_initialization;text:window.v.wizardPlan.resume_message||"" }
                        Body { visible:window.v.bootStoreMode;text:window.v.wizardPlan.target||"";color:window.ink }
                        Body { visible:window.v.bootStoreMode;text:window.v.wizardPlan.maintenance_message||"" }
                        Body { visible:!window.v.bootStoreMode&&!!window.v.wizardPlan.boot_store_retention;text:"完成后自动回收过期引导，保留现用系统和最近可核验的 Linux 内核回退版本。存在未完成事务时暂停清理。" }
                        Body { visible:window.v.bootStoreMode&&window.v.wizardPlan.reclaimable_bytes>0;text:"预计回收 "+window.gib(window.v.wizardPlan.reclaimable_bytes)+"。现用系统和最近可核验的内核回退材料继续保留。" }
                        Repeater { model:window.v.wizardPlan.maintenance_files||[];delegate:Body { required property var modelData;text:modelData.path+(modelData.source ? " → "+modelData.source : "（过期）");wrapMode:Text.WrapAnywhere } }
                        Body { text:window.v.message;visible:window.v.busy }
                        ProgressBar { visible:window.v.busy;Layout.fillWidth:true;indeterminate:window.v.progress<0;value:Math.max(0,window.v.progress) }
                        Body { visible:!window.v.busy;text:"设备："+(window.v.wizardPlan.serial||"") }
                        Body {
                            visible: window.v.bootSwitchMode && !!window.v.wizardPlan.plan_id
                            text: "当前默认启动为 " + (window.v.wizardPlan.previous_name || "未登记")
                                + "，本次将 " + window.v.wizardPlan.target + " 设为默认。"
                        }
                        Body {
                            visible: window.v.bootSwitchMode && !!window.v.wizardPlan.plan_id
                            text: "备份当前引导并写入 A 槽启动镜像。回读通过后更新系统登记。"
                        }
                        Body { visible:!!window.v.wizardPlan.rom;text:"Android ROM："+(window.v.wizardPlan.rom||"") }
                        Body { visible:!!window.v.wizardPlan.rom_admission;text:window.v.wizardPlan.rom_admission ? window.v.wizardPlan.rom_admission.warning : "";color:"#94481e" }
                        Body { visible:(window.v.wizardPlan.android_writes||[]).length>0;text:"ROM 写入："+(window.v.wizardPlan.android_writes||[]).join("、") }
                        Body { visible:!!window.v.wizardPlan.full_rom;text:"完整 ROM 会更新共享固件；未选择的 Linux rootfs 和已有引导文件保留，但后续启动兼容性仍需验证。";color:"#94481e" }
                        Body { objectName:"fixedRecoveryPlan";visible:!!window.v.wizardPlan.fixed_recovery;text:window.v.wizardPlan.fixed_recovery ? window.v.wizardPlan.fixed_recovery.message : "" }
                        Body { visible:!!window.v.wizardPlan.fixed_recovery;text:"固定 Rec 仅处理 recovery_a；recovery_b 保留。安装前后核验启动链和整分区摘要，失败留在当前安装环境。新 ROM 下的冷启动仍需实机确认。" }
                        Body { visible:!!window.v.wizardPlan.rom&&window.v.wizardPlan.fixed_recovery_policy==="rom";text:"Recovery：按 ROM 清单写入，可能替换本机固定 Rec。";color:"#94481e" }
                        Repeater { model:window.v.wizardPlan.installations||[];delegate:Body { required property var modelData;text:(modelData.kind==="android" ? "Android" : modelData.name||"Linux")+" → "+modelData.target+(modelData.set_default ? "（最后写入默认引导）" : "") } }
                        Body { visible:(window.v.wizardPlan.preserved_roots||[]).length>0;text:"不安装的 Linux rootfs："+(window.v.wizardPlan.preserved_roots||[]).join("、") }
                        Repeater { model:window.v.wizardPlan.additions||[];delegate:Body { required property var modelData;text:"新建 "+modelData.name+" · "+window.gib(modelData.bytes) } }
                        Repeater { model:window.v.wizardPlan.destructive||[];delegate:Body { required property var modelData;text:modelData.action+"："+modelData.name+" · "+window.gib(modelData.bytes);color:"#a34b24" } }
                        Body { visible:!!window.v.wizardPlan.set_default&&!window.v.bootSwitchMode;text:"最终默认引导："+(window.v.wizardPlan.default_target||window.v.wizardPlan.target||"") }
                        Body { visible:!!window.v.wizardPlan.default_removed;text:window.v.wizardPlan.set_default ? "本次清除范围包含原默认系统。新默认引导将在最后写入。" : "本次清除范围包含原默认系统，旧引导可能无法启动。"+(window.v.wizardPlan.native_install ? "执行结束后将留在 Recovery。" : "");color:"#a34b24" }
                        Body { text:"备份："+(window.v.wizardPlan.backup_dir||"");visible:!!window.v.wizardPlan.plan_id }
                        Body { visible:!!window.v.wizardPlan.backup_reused;text:"基础设备备份已核验并复用，本次当前引导快照已另存。" }
                        Body { visible:!!window.v.wizardPlan.rom_backup_dir;text:"ROM 快照："+window.v.wizardPlan.rom_backup_dir+"（复用 "+String(window.v.wizardPlan.rom_backups_reused||0)+" 个分区镜像）";wrapMode:Text.WrapAnywhere }
                        Body { visible:!!window.v.wizardPlan.native_install&&!window.v.bootSwitchMode;text:"本次由原生 Rec 执行受控写入并回读。完成后留在 Rec，不自动重启或切槽；系统首次启动尚需另行验证。";color:"#94481e" }
                        CheckBox {
                            id: consent
                            objectName: "operationConsent"
                            visible: !!window.v.wizardPlan.plan_id
                            text: window.v.bootStoreMode ? "我确认执行以上引导存储或中断恢复操作" : window.v.bootSwitchMode
                                ? "我确认切换下次开机的默认启动系统"
                                : "个人资料已另行备份，确认以上分区与刷写操作（当前实现尚未完成真机验收）"
                            Layout.fillWidth: true
                        }
                        CheckBox { id:experimental;visible:false;checked:consent.checked }
                        TextField { id:serial;objectName:"confirmationSerial";visible:!!window.v.wizardPlan.plan_id;Layout.fillWidth:true;text:window.v.selectedSerial;readOnly:true;placeholderText:"目标设备序列号" }
                    }
                }
                Page { Card { Layout.fillWidth:true;Heading { text:"正在执行" }Body { text:window.v.message }ProgressBar { Layout.fillWidth:true;indeterminate:window.v.progress<0;value:Math.max(0,window.v.progress) }Body { text:"保持 USB 连接。写入开始后请等待操作结束。" } } }
                Page {
                    Card {
                        Layout.fillWidth:true
                        Heading {
                            text: window.v.bootStoreMode ? (window.v.receipt.completed ? "引导维护已完成" : "引导维护已停止") : window.v.bootSwitchMode
                                ? (window.v.receipt.completed ? "已切换默认系统" : "切换已停止")
                                : (window.v.receipt.completed ? "操作已完成" : "操作已停止")
                        }
                        Body {
                            visible: window.v.bootSwitchMode
                            text: window.v.receipt.completed
                                ? window.v.wizardPlan.target + " 已设为默认。请在平板的 Recovery 中选择「重启到默认系统」。"
                                : "请保留当前 Recovery 和执行记录，先核对引导状态。"
                        }
                        Body { visible:window.v.bootStoreMode;text:window.v.receipt.completed ? "已回读核验并保存维护记录。可重新检查引导存储。" : "保持 Recovery，重新检查当前状态后再生成恢复计划。" }
                        Body { visible:!!window.v.receipt.boot_store_retention&&window.v.receipt.boot_store_retention.status==="deferred";text:"系统操作已完成，引导历史清理暂缓："+((window.v.receipt.boot_store_retention||{}).message||"");color:"#94481e" }
                        Body { visible:!window.v.bootSwitchMode&&!window.v.bootStoreMode;text:window.v.receipt.completed ? "执行结果已保存。系统或 Recovery 的首次启动仍需在平板上验证。" : window.v.receipt.android_rom_written&&window.v.receipt.boot_finalization_pending ? "Android 系统已写入，默认引导尚未确认。请保留本次记录并核对对应的 boot.img；重新安装会再次清空 Android 数据。" : window.v.receipt.android_commit_pending ? "Android 已写入，系统登记尚未完成。请保留本次记录，核对引导存储后继续。" : "保留当前安装环境和执行记录，不要直接重启或重复分区。先只读核对分区表和已完成阶段；已清除的个人数据不会自动恢复。" }
                        Body { text:"记录目录："+(window.v.receipt.run_dir||"") }
                        Action { text:"保存报告";onClicked:reportDialog.open() }
                        Action { text:"检查引导存储与中断恢复";enabled:!!window.v.selectedSerial&&!window.v.busy;onClicked:backend.inspectBootStore() }
                        Action {
                            objectName:"resumePartitionInit"
                            text:"续接分区初始化（选择原记录）"
                            visible:!window.v.bootSwitchMode&&window.v.receipt.failed===true&&window.v.receipt.write_dispatched===true
                            enabled:!!window.v.selectedSerial&&!window.v.busy
                            onClicked:resumeDialog.open()
                        }
                        Action { text:"返回分区表";enabled:!window.v.busy;onClicked:backend.scanDevice() }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth:true
                Action { objectName:"wizardBack";visible:window.v.wizardStep===1||window.v.wizardStep===2;text:window.v.bootSwitchMode||window.v.bootStoreMode ? "返回分区表" : "上一步";enabled:!window.v.busy;onClicked:backend.back(window.v.bootSwitchMode||window.v.bootStoreMode ? 0 : window.v.wizardStep-1) }
                Action { visible:window.v.canCancel;text:"取消准备";onClicked:backend.cancel() }
                Item { Layout.fillWidth:true }
                Action {
                    objectName:"partitionNext"
                    visible:window.v.wizardStep===0
                    text: window.v.inspectionOnly && window.v.targetKind==="android"
                        ? "下一步：ROM 分析 →"
                        : window.hasPartitionEdits && !window.v.targetKind
                            ? "下一步：分区方案 →"
                            : "下一步：安装材料 →"
                    primary:true
                    enabled:!!window.v.inventorySummary.snapshot_id && !window.v.busy
                        && (window.v.inspectionOnly ? window.v.targetKind==="android" : !!window.v.targetKind || window.hasPartitionEdits)
                    onClicked:backend.nextFromPartitions()
                }
                Action { objectName:"materialsNext";visible:window.v.wizardStep===1;text:window.v.romPreviewMode ? "分析 ROM 与安装后 Rec 条件 →" : "下一步：检查安装方案 →";primary:true;enabled:!window.v.busy;onClicked:backend.prepareWorkflow() }
                Action { objectName:"executeButton";visible:window.v.wizardStep===2&&!window.v.romPreviewMode;text:window.v.bootSwitchMode ? "确认切换" : "确认并执行";primary:true;enabled:!window.v.busy&&window.v.state==="prepared"&&consent.checked&&experimental.checked&&serial.text.trim()===window.v.wizardPlan.serial;onClicked:backend.executeWorkflow(serial.text,consent.checked,experimental.checked) }
            }
        }
    }
}
