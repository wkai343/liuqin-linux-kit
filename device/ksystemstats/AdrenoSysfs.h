// SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
#pragma once

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QList>
#include <QString>
#include <QStringList>
#include <optional>

namespace AdrenoSysfs
{
struct Device {
    QString path;
    QString name;
    QString frequencyPath;
};

inline QString availableGpuId(const QStringList &occupied)
{
    for (qsizetype index = 0; index <= occupied.size(); ++index) {
        const auto id = QStringLiteral("gpu%1").arg(index);
        if (!occupied.contains(id)) {
            return id;
        }
    }
    return {}; // Unreachable: N occupied IDs cannot fill N+1 candidates.
}

inline QByteArray read(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    return file.read(4096);
}

inline std::optional<qulonglong> number(const QString &path)
{
    auto text = read(path).trimmed();
    if (text.isEmpty() || text.size() > 20) {
        return std::nullopt;
    }
    for (auto ch : text) {
        if (ch < '0' || ch > '9') {
            return std::nullopt;
        }
    }
    bool ok = false;
    auto value = text.toULongLong(&ok);
    return ok ? std::optional<qulonglong>{value} : std::nullopt;
}

inline std::optional<unsigned int> usage(const QString &path)
{
    auto value = number(path + QStringLiteral("/gpu_busy_percent"));
    if (!value || *value > 100) {
        return std::nullopt;
    }
    return static_cast<unsigned int>(*value);
}

inline QList<Device> discover(const QString &platformRoot = QStringLiteral("/sys/bus/platform/devices"))
{
    QList<Device> devices;
    QDir directory(platformRoot);
    // GPU platform devices are symlinks in sysfs. No fixed GPU address or
    // DRM card number: the display controller can own the primary DRM node.
    for (const auto &entry : directory.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
        const auto compatible = read(entry.filePath() + QStringLiteral("/of_node/compatible")).split('\0');
        bool adreno = false;
        QString name = QStringLiteral("Adreno");
        for (const auto &item : compatible) {
            if (item == "qcom,adreno" || item.startsWith("qcom,adreno-")) {
                adreno = true;
                if (item.startsWith("qcom,adreno-")) {
                    name = QStringLiteral("Adreno ") + QString::fromLatin1(item.mid(12));
                }
            }
        }
        if (!adreno || !usage(entry.filePath())) {
            continue;
        }
        const auto path = entry.canonicalFilePath();
        if (path.isEmpty()) {
            continue;
        }
        bool duplicate = false;
        for (const auto &device : devices) {
            duplicate |= device.path == path;
        }
        if (duplicate) {
            continue;
        }
        QString frequency;
        const QDir devfreq(path + QStringLiteral("/devfreq"));
        for (const auto &child : devfreq.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
            auto candidate = child.filePath() + QStringLiteral("/cur_freq");
            if (number(candidate)) {
                frequency = candidate;
                break;
            }
        }
        devices.append({path, name, frequency});
    }
    return devices;
}
}
