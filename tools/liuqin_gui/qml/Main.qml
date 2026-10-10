import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "installerWindow"
    width: 1180
    height: 800
    minimumWidth: 1020
    minimumHeight: 720
    visible: true
    title: "Liuqin · Linux 安装助手"
    color: "#f3f5f8"
    font.family: "Noto Sans CJK SC"
    font.pixelSize: 14
    property var v: backend.view
    property color ink: "#172b3a"
    property color muted: "#647684"
    property color accent: "#087f83"
    property var titles: ["准备安装", "连接平板", "安装方案", "确认安装", "安装进度", "安装结果"]
    property var subtitles: ["选择完整安装包，并准备平板以外的备份。", "选择平板，独立创建或复用设备备份。", "依据真实容量生成方案，复用已核验的备份。", "核对设备和清除范围，再开始写入。", "保持平板连接，等待各阶段完成。", "在平板上完成首次设置，并验证正常重启。"]
    property string confirmedPlan: ""
    onClosing: function(close) { close.accepted = backend.closeWindow() }

    Connections {
        target: backend
        function onChanged() {
            const plan = backend.view.plan
            if (plan.plan_id && plan.plan_id !== window.confirmedPlan) {
                window.confirmedPlan = plan.plan_id
                backedUp.checked = false
                erase.checked = false
                experimental.checked = false
                serialConfirmation.text = ""
                firstBoot.checked = false
            }
        }
    }

    function gib(bytes) { return (Number(bytes || 0) / 1073741824).toFixed(2) + " GiB" }

    component Action: Button {
        property bool primary: false
        implicitHeight: 44
        leftPadding: 20
        rightPadding: 20
        contentItem: Text {
            text: parent.text
            font: parent.font
            color: !parent.enabled ? "#95a2aa" : parent.primary ? "white" : window.ink
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 8
            color: !parent.enabled ? "#e9edf0" : parent.primary ? (parent.down ? "#07676a" : window.accent) : "white"
            border.color: parent.primary ? "transparent" : "#d8e1e6"
        }
    }
    component Panel: Rectangle {
        id: panel
        default property alias contents: panelColumn.data
        implicitHeight: panelColumn.implicitHeight + 40
        color: "white"
        border.color: "#e0e7ec"
        radius: 12
        ColumnLayout {
            id: panelColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 20
            spacing: 14
        }
    }
    component Heading: Text {
        color: window.ink
        font.pixelSize: 18
        font.weight: Font.DemiBold
    }
    component Body: Text {
        color: window.muted
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        lineHeight: 1.35
    }
    component Detail: RowLayout {
        property string label
        property string value
        Layout.fillWidth: true
        Text { text: parent.label; color: window.muted; Layout.preferredWidth: 150 }
        Text { text: parent.value; color: window.ink; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
    }
    component Page: ScrollView {
        id: pageScroll
        default property alias contents: pageColumn.data
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            id: pageColumn
            width: pageScroll.availableWidth
            spacing: 18
        }
    }

    FolderDialog { id: bundleDialog; title: "选择完整安装包目录"; onAccepted: backend.setBundlePath(selectedFolder.toString()) }
    FolderDialog { id: backupDialog; title: "选择电脑上的备份位置"; onAccepted: backend.setBackupPath(selectedFolder.toString()) }
    FolderDialog { id: existingBackupDialog; title: "选择已有设备备份目录"; onAccepted: backend.selectBackup(selectedFolder.toString()) }
    FolderDialog { id: reportDialog; title: "保存诊断报告"; onAccepted: backend.exportReport(selectedFolder.toString()) }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.preferredWidth: 240
            Layout.fillHeight: true
            color: "#112734"
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 24
                spacing: 8
                Rectangle {
                    Layout.topMargin: 8
                    width: 42; height: 48; radius: 10; color: "#1b424c"
                    Rectangle { anchors.centerIn: parent; width: 20; height: 29; radius: 4; color: "transparent"; border.color: "#8edbd1"; border.width: 2 }
                }
                Text { text: "LIUQIN"; color: "#b0e3df"; font.pixelSize: 12; font.letterSpacing: 3; Layout.topMargin: 12 }
                Text { text: "Linux 安装助手"; color: "white"; font.pixelSize: 23; font.weight: Font.DemiBold }
                Text { text: "小米平板 6 Pro"; color: "#94aebb"; Layout.bottomMargin: 26 }
                Repeater {
                    model: window.titles
                    delegate: Rectangle {
                        required property string modelData
                        required property int index
                        Layout.fillWidth: true
                        height: 52
                        radius: 8
                        color: index === window.v.page ? "#214650" : "transparent"
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 12; spacing: 12
                            Rectangle {
                                width: 25; height: 25; radius: 13
                                color: index === window.v.page ? "#a1e1d8" : "#29414e"
                                Text { anchors.centerIn: parent; text: index + 1; font.pixelSize: 12; color: index === window.v.page ? "#123a40" : "#a8bcc8" }
                            }
                            Text { text: modelData; color: index === window.v.page ? "white" : "#a8bcc8"; Layout.fillWidth: true }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#29414e" }
                Text { text: "单根分区 · A 槽引导"; color: "#a8bcc8"; Layout.topMargin: 12 }
                Text { text: "第一版  /  0.2.1"; color: "#688896"; font.pixelSize: 12; Layout.bottomMargin: 8 }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 32
            spacing: 18
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    spacing: 8
                    Text { text: window.titles[window.v.page]; color: window.ink; font.pixelSize: 29; font.weight: Font.DemiBold }
                    Text { text: window.subtitles[window.v.page]; color: window.muted }
                }
                Item { Layout.fillWidth: true }
                Rectangle {
                    width: badge.implicitWidth + 26; height: 32; radius: 16
                    color: window.v.simulation ? "#e3f3f0" : "#e9edf2"
                    Text { id: badge; anchors.centerIn: parent; text: window.v.simulation ? "演示模式 · 不连接设备" : "真机模式"; color: window.v.simulation ? "#176e67" : "#4b6173"; font.pixelSize: 12 }
                }
            }
            Rectangle {
                visible: !!window.v.error
                Layout.fillWidth: true
                implicitHeight: errorText.implicitHeight + 24
                radius: 8
                color: "#fff1e9"
                Text { id: errorText; anchors.fill: parent; anchors.margins: 12; text: window.v.error; color: "#99421b"; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: window.v.page

                Page {
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "安装材料" }
                        Body { text: window.v.simulation ? "使用内置模拟数据走完整个安装流程。不会访问 USB，也不会修改平板。" : "选择包含系统归档、临时安装环境和启动镜像的完整目录。程序会逐项核验文件。" }
                        RowLayout {
                            Layout.fillWidth: true
                            TextField { Layout.fillWidth: true; implicitHeight: 44; text: window.v.bundlePath; placeholderText: window.v.simulation ? "内置演示安装包" : "安装包目录"; readOnly: true; selectByMouse: true }
                            Action { text: "选择目录"; enabled: !window.v.busy && !window.v.simulation; onClicked: bundleDialog.open() }
                        }
                        RowLayout {
                            Action { text: "检查安装包"; enabled: window.v.workerReady && !window.v.busy; onClicked: backend.validateBundle() }
                            Text { text: window.v.bundleValid ? "✓ 文件完整，支持单根方案" : window.v.busy ? window.v.message : "等待检查"; color: window.v.bundleValid ? window.accent : window.muted; Layout.fillWidth: true }
                        }
                        ProgressBar { visible: window.v.busy; Layout.fillWidth: true; value: Math.max(0, window.v.progress); indeterminate: window.v.progress < 0 }
                        Detail { visible: window.v.bundleValid; label: "内核版本"; value: window.v.bundleInfo.kernel || "" }
                    }
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "新备份保存位置" }
                        Body { text: "设备备份是独立操作，已有备份可重复使用。这里保存引导、校准信息和分区表；个人文件仍需另行备份到平板以外。" }
                        RowLayout {
                            Layout.fillWidth: true
                            TextField { Layout.fillWidth: true; implicitHeight: 44; text: window.v.backupPath; readOnly: true; selectByMouse: true }
                            Action { text: "更改位置"; enabled: !window.v.busy; onClicked: backupDialog.open() }
                        }
                    }
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "这次安装什么" }
                        Body { text: "将 Linux 安装到原数据分区的全部空间。系统和个人目录共享一个文件系统，并从 A 槽启动。" }
                    }
                }

                Page {
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: window.v.simulation ? "模拟设备已连接" : "让平板进入 Fastboot" }
                        Body { text: "Bootloader 需要已解锁。通常可关机后同时按住电源键和音量下键，再通过 USB 连接电脑。" }
                        RowLayout {
                            Action { text: "刷新设备"; enabled: !window.v.busy; onClicked: backend.probeDevices() }
                            Text { text: "找到 " + window.v.devices.length + " 台设备"; color: window.muted }
                        }
                        Repeater {
                            model: window.v.devices
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                height: 76
                                radius: 9
                                color: window.v.selectedSerial === modelData.serial ? "#eef8f6" : "#f7f9fb"
                                border.color: window.v.selectedSerial === modelData.serial ? window.accent : "#e1e7ec"
                                Column { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; anchors.leftMargin: 18; spacing: 6
                                    Text { text: modelData.serial; color: window.ink; font.weight: Font.DemiBold }
                                    Text { text: modelData.description; color: window.muted; font.pixelSize: 12 }
                                }
                                Text { anchors.right: parent.right; anchors.rightMargin: 18; anchors.verticalCenter: parent.verticalCenter; text: window.v.selectedSerial === modelData.serial ? "已选择" : "选择"; color: window.accent }
                                MouseArea { anchors.fill: parent; enabled: !window.v.busy; onClicked: backend.selectDevice(modelData.serial) }
                            }
                        }
                        Body { visible: window.v.devices.length === 0; text: "未发现设备。检查 USB 连接、Fastboot 状态及电脑访问设备的权限，再点击刷新。" }
                    }
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "设备备份 · 独立操作" }
                        Body { text: "可以选择已有备份，或单独创建一次。安装准备只核验并复用，不再重复传输备份，也不会覆盖原始备份。" }
                        RowLayout {
                            Layout.fillWidth: true
                            TextField { Layout.fillWidth: true; implicitHeight: 44; text: window.v.selectedBackupPath; placeholderText: "尚未选择设备备份"; readOnly: true; selectByMouse: true }
                            Action { text: "选择已有备份"; enabled: !!window.v.selectedSerial && !window.v.busy; onClicked: existingBackupDialog.open() }
                        }
                        RowLayout {
                            Action { objectName: "createBackupButton"; text: "创建设备备份"; enabled: !!window.v.selectedSerial && !window.v.busy; onClicked: backend.createBackup(false) }
                            Action { text: "查找已有备份"; enabled: !!window.v.selectedSerial && !window.v.busy; onClicked: backend.findExistingBackup() }
                            Action { visible: !!window.v.selectedBackupPath || window.v.backupCandidates.length > 0; text: "另建备份"; enabled: !window.v.busy; onClicked: backend.createBackup(true) }
                        }
                        Body { text: window.v.backupInfo.valid_local ? (window.v.backupInfo.source === "created" ? "新备份已完成并核验。" : "已有备份完整性检查通过，可用于后续安装。") : "请先准备设备备份。个人文件不包含在这份备份内。"; color: window.v.backupInfo.valid_local ? window.accent : window.muted }
                        Body { visible: window.v.busy; text: window.v.message }
                        ProgressBar { visible: window.v.busy; Layout.fillWidth: true; value: Math.max(0, window.v.progress); indeterminate: window.v.progress < 0 }
                    }
                }

                Page {
                    Panel {
                        visible: window.v.state !== "prepared"
                        Layout.fillWidth: true
                        Heading { text: window.v.state === "failed" ? "准备已停止" : "正在准备真实安装方案" }
                        Body { text: window.v.message }
                        ProgressBar { visible: window.v.state !== "failed"; Layout.fillWidth: true; value: Math.max(0, window.v.progress); indeterminate: window.v.progress < 0 }
                        Body { text: window.v.state === "failed" ? "此次准备未下发格式化。保存报告，让平板回到 Fastboot 后，再重新检查。" : "准备阶段可以有序取消。原始数据分区保持不变。" }
                        Action { visible: window.v.state === "failed"; text: "保存诊断报告"; onClicked: reportDialog.open() }
                    }
                    Panel {
                        visible: window.v.state === "prepared"
                        Layout.fillWidth: true
                        Heading { text: "单根分区方案" }
                        Rectangle {
                            Layout.fillWidth: true; height: 104; radius: 9; color: "#e4f3ef"
                            Column { anchors.centerIn: parent; spacing: 8
                                Text { anchors.horizontalCenter: parent.horizontalCenter; text: "根分区 /"; color: "#18665e"; font.pixelSize: 20; font.weight: Font.DemiBold }
                                Text { anchors.horizontalCenter: parent.horizontalCenter; text: window.gib(window.v.plan.root_bytes); color: "#367c72" }
                            }
                        }
                        Body { text: "使用原数据分区的全部空间，保留分区边界。系统与用户目录都在这个根文件系统中，/home 是普通目录，不创建独立分区。" }
                        Detail { label: "设备序列号"; value: window.v.plan.serial || "" }
                        Detail { label: "当前电量"; value: (window.v.device.battery || "—") + "%" }
                        Detail { label: "引导方案"; value: "写入 boot_a，选择 A 槽" }
                        Detail { label: "设备备份"; value: window.v.plan.backup_verified ? "复用已有备份，核验通过" : "等待核验" }
                        Detail { label: "备份目录"; value: window.v.plan.backup_dir || "" }
                        Body { visible: !!window.v.plan.warnings && window.v.plan.warnings.length > 0; text: (window.v.plan.warnings || []).join("\n"); color: "#8c5b19" }
                    }
                    Panel {
                        visible: window.v.state === "prepared"
                        Layout.fillWidth: true
                        Heading { text: "安装将清除什么" }
                        Body { text: "整个原数据分区会被格式化，包括 Android 的个人数据，以及此前存放在该分区上的 Linux 系统和用户文件。原始分区备份不包含这些个人文件。" }
                    }
                }

                Page {
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "最后核对" }
                        Detail { label: "目标设备"; value: window.v.plan.serial || "" }
                        Detail { label: "Linux 空间"; value: window.gib(window.v.plan.root_bytes) }
                        Detail { label: "备份"; value: "原始分区备份已核验；个人文件需自行备份" }
                        Body { visible: !!window.v.plan.warnings && window.v.plan.warnings.length > 0; text: (window.v.plan.warnings || []).join("\n"); color: "#8c5b19" }
                        Body { text: "开始后会格式化数据分区并安装 Linux。请核对序列号，在下面完成确认。" }
                        CheckBox { id: backedUp; objectName: "backedUpCheck"; text: "个人文件已备份到平板外，或没有需要保留的数据"; Layout.fillWidth: true }
                        CheckBox { id: erase; objectName: "eraseCheck"; text: "我确认清空整个数据分区并安装这份方案"; Layout.fillWidth: true }
                        CheckBox { id: experimental; objectName: "experimentalCheck"; visible: !!window.v.plan.experimental; text: "我接受使用尚未完成全部设备验收的实验安装包"; Layout.fillWidth: true }
                        Body { text: "输入目标设备序列号：" }
                        TextField { id: serialConfirmation; objectName: "confirmationSerial"; Layout.fillWidth: true; implicitHeight: 44; placeholderText: window.v.plan.serial || ""; selectByMouse: true }
                    }
                }

                Page {
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: window.v.message }
                        Body { text: window.v.progress >= 0 ? "当前阶段：" + Math.round(window.v.progress * 100) + "%" : "此阶段没有可测量的百分比，请等待完成。" }
                        ProgressBar { Layout.fillWidth: true; value: Math.max(0, window.v.progress); indeterminate: window.v.progress < 0 }
                        Body { text: "安装正在进行，无法直接取消。请保持 USB 连接和电脑电源。" }
                    }
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "操作记录" }
                        TextArea { Layout.fillWidth: true; text: window.v.log; readOnly: true; selectByMouse: true; wrapMode: Text.WrapAnywhere; color: window.muted; textFormat: Text.PlainText; background: null }
                    }
                }

                Page {
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: window.v.state === "verified" ? "启动验证已完成" : window.v.state === "failed" ? "安装已停止" : "系统与引导已写入" }
                        Body { text: window.v.state === "failed" ? "先查看执行记录和当前平板状态。不要直接重跑整个安装流程，以免重复清空数据。" : window.v.state === "verified" ? "已根据你的实际测试，记录桌面和正常重启通过。" : "等待平板进入首次设置。短暂黑屏可能是启动过渡；请先等待，再判断是否失败。" }
                        Detail { label: "根系统安装"; value: window.v.receipt.root_installed ? "已完成" : "未确认" }
                        Detail { label: "A 槽引导回读"; value: window.v.receipt.boot_readback_verified ? "校验通过" : "未确认" }
                        Detail { label: "首次启动"; value: window.v.receipt.first_boot_verified ? "用户已验证" : "等待实际验证" }
                        Detail { label: "执行记录目录"; value: window.v.receipt.run_dir || "" }
                        CheckBox { id: firstBoot; objectName: "firstBootCheck"; visible: window.v.state === "awaiting_first_boot"; text: "我已在平板进入桌面，并正常重启后再次进入桌面"; Layout.fillWidth: true }
                        Action { text: "保存诊断报告"; onClicked: reportDialog.open() }
                    }
                    Panel {
                        Layout.fillWidth: true
                        Heading { text: "操作记录" }
                        TextArea { Layout.fillWidth: true; text: window.v.log; readOnly: true; selectByMouse: true; wrapMode: Text.WrapAnywhere; color: window.muted; textFormat: Text.PlainText; background: null }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: "#dce4e9" }
            RowLayout {
                Layout.fillWidth: true
                Action { visible: window.v.page === 1 || window.v.page === 3; text: "上一步"; enabled: !window.v.busy; onClicked: backend.goPage(window.v.page === 3 ? 2 : 0) }
                Action { visible: window.v.page === 2 && window.v.state === "failed"; text: "返回准备页"; enabled: !window.v.busy; onClicked: backend.goPage(0) }
                Action { visible: window.v.state === "preparing" || window.v.state === "backing_up" || window.v.state === "prepared"; text: window.v.state === "backing_up" ? "取消备份" : "取消准备"; onClicked: backend.cancel() }
                Item { Layout.fillWidth: true }
                Action { visible: window.v.page === 0; text: "连接平板 →"; primary: true; enabled: window.v.bundleValid && !window.v.busy; onClicked: backend.goPage(1) }
                Action { objectName: "prepareButton"; visible: window.v.page === 1; text: "检查安装方案 →"; primary: true; enabled: !!window.v.selectedSerial && !!window.v.selectedBackupPath && window.v.backupInfo.valid_local && window.v.backupInfo.serial === window.v.selectedSerial && !window.v.busy; onClicked: backend.prepare() }
                Action { visible: window.v.page === 2 && window.v.state === "prepared"; text: "审核安装方案 →"; primary: true; enabled: !window.v.busy; onClicked: backend.goPage(3) }
                Action {
                    objectName: "installButton"
                    visible: window.v.page === 3
                    text: window.v.simulation ? "开始演示安装" : "清除数据并安装"
                    primary: true
                    enabled: !window.v.busy && window.v.state === "prepared" && backedUp.checked && erase.checked &&
                             (!window.v.plan.experimental || experimental.checked) && serialConfirmation.text.trim() === window.v.plan.serial
                    onClicked: backend.install(serialConfirmation.text, backedUp.checked, erase.checked, experimental.checked)
                }
                Action { visible: window.v.page === 4; text: "安装进行中"; enabled: false }
                Action { visible: window.v.page === 5 && window.v.state === "awaiting_first_boot"; text: "记录启动验证通过"; primary: true; enabled: firstBoot.checked && !window.v.busy; onClicked: backend.verifyFirstBoot() }
            }
        }
    }
}
