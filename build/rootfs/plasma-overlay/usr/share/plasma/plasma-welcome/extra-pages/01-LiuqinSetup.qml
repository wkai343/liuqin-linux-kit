// SPDX-License-Identifier: MIT
pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.welcome as Welcome

Welcome.Page {
    id: root
    heading: "设置你的平板"
    description: "按照需要打开下面的设置。完成后返回欢迎中心继续；也可以跳过，稍后从系统设置中修改。"
    property string launchError: ""

    topContent: [
        QQC2.Label {
            Layout.fillWidth: true
            text: root.launchError
            visible: text.length > 0
            wrapMode: Text.WordWrap
        }
    ]

    function openSettings(module) {
        root.launchError = "";
        Welcome.Utils.runCommand("systemsettings " + module, function(status, output) {
            if (status !== 0) {
                root.launchError = "无法打开设置，请从应用菜单进入系统设置。";
            }
        });
    }

    QQC2.ScrollView {
        id: scroll
        anchors.fill: parent
        contentWidth: availableWidth
        QQC2.ScrollBar.horizontal.policy: QQC2.ScrollBar.AlwaysOff

        ColumnLayout {
            width: scroll.availableWidth
            spacing: Kirigami.Units.largeSpacing

            Repeater {
                model: [
                    { title: "语言与格式", description: "选择界面语言、日期和数字格式。简体中文语言包与中文字体已预装；更改语言后通常需要重新登录。", icon: "preferences-desktop-locale", module: "kcm_regionandlang" },
                    { title: "网络连接", description: "连接 Wi-Fi 或管理已有连接。联网后可以安装软件和获取系统更新。", icon: "network-wireless", module: "kcm_networkmanagement" },
                    { title: "日期与时间", description: "选择时区并检查日期、时间和自动校时设置。", icon: "preferences-system-time", module: "kcm_clock" },
                    { title: "账号与密码", description: "修改当前账号的显示名称、头像或密码。修改系统设置时可能需要输入你登录时使用的密码。", icon: "preferences-system-users", module: "kcm_users" }
                ]

                delegate: QQC2.Frame {
                    id: card
                    required property var modelData
                    Layout.fillWidth: true

                    ColumnLayout {
                        width: parent.width
                        spacing: Kirigami.Units.smallSpacing

                        Kirigami.Heading {
                            Layout.fillWidth: true
                            level: 3
                            text: card.modelData.title
                            wrapMode: Text.WordWrap
                        }
                        QQC2.Label {
                            Layout.fillWidth: true
                            text: card.modelData.description
                            wrapMode: Text.WordWrap
                        }
                        QQC2.Button {
                            objectName: card.modelData.module
                            text: "打开" + card.modelData.title
                            icon.name: card.modelData.icon
                            onClicked: root.openSettings(card.modelData.module)
                        }
                    }
                }
            }

            QQC2.Label {
                Layout.fillWidth: true
                text: "关闭欢迎中心后，仍可从应用菜单重新打开“欢迎中心”。"
                wrapMode: Text.WordWrap
            }
        }
    }
}
