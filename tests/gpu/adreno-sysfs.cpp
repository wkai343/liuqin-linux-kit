// SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
#include "AdrenoSysfs.h"
#include <QCoreApplication>
#include <QTemporaryDir>
#include <QRegularExpression>
#include <cassert>
#include <iostream>

static void write(const QString &path, const QByteArray &value)
{
    assert(QDir{}.mkpath(QFileInfo(path).path()));
    QFile file(path);
    assert(file.open(QIODevice::WriteOnly));
    assert(file.write(value) == value.size());
}

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    QTemporaryDir temporary;
    assert(temporary.isValid());
    const auto root = temporary.path();
    const auto platform = root + QStringLiteral("/bus/platform/devices");
    const auto firstId = AdrenoSysfs::availableGpuId({});
    assert(firstId == QStringLiteral("gpu0"));
    assert(AdrenoSysfs::availableGpuId({QStringLiteral("gpu0"), QStringLiteral("gpu2")}) == QStringLiteral("gpu1"));
    assert(AdrenoSysfs::availableGpuId({QStringLiteral("gpu1")}) == QStringLiteral("gpu0"));
    // Match the shipped history page's numbered GPU sensor selection.
    assert(QRegularExpression(QStringLiteral("^gpu/gpu[0-9]+/usage$")).match(QStringLiteral("gpu/") + firstId + QStringLiteral("/usage")).hasMatch());
    assert(QDir{}.mkpath(platform));
    const auto gpu = root + QStringLiteral("/devices/soc/3d00000.gpu");
    write(gpu + QStringLiteral("/of_node/compatible"), QByteArray("qcom,adreno-730.1\0qcom,adreno\0", 29));
    write(gpu + QStringLiteral("/gpu_busy_percent"), "0\n");
    write(gpu + QStringLiteral("/devfreq/3d00000.gpu/cur_freq"), "220000000\n");
    assert(QFile::link(gpu, platform + QStringLiteral("/3d00000.gpu")));
    assert(QFile::link(gpu, platform + QStringLiteral("/duplicate-alias")));
    auto devices = AdrenoSysfs::discover(platform);
    assert(devices.size() == 1);
    assert(devices[0].name == QStringLiteral("Adreno 730.1"));
    assert(AdrenoSysfs::usage(devices[0].path).value() == 0);
    assert(AdrenoSysfs::number(devices[0].frequencyPath).value() == 220000000);
    write(gpu + QStringLiteral("/gpu_busy_percent"), " 42\n");
    assert(AdrenoSysfs::usage(gpu).value() == 42);
    write(gpu + QStringLiteral("/gpu_busy_percent"), "100\n");
    assert(AdrenoSysfs::usage(gpu).value() == 100);
    for (const auto &value : {"", "101", "-1", "1.5", "abc", "0x10", "18446744073709551616", "35 20"}) {
        write(gpu + QStringLiteral("/gpu_busy_percent"), value);
        assert(!AdrenoSysfs::usage(gpu));
        assert(AdrenoSysfs::discover(platform).isEmpty());
    }
    write(gpu + QStringLiteral("/gpu_busy_percent"), "7\n");
    assert(QFile::remove(gpu + QStringLiteral("/devfreq/3d00000.gpu/cur_freq")));
    devices = AdrenoSysfs::discover(platform);
    assert(devices.size() == 1 && devices[0].frequencyPath.isEmpty());
    write(gpu + QStringLiteral("/of_node/compatible"), "other,gpu\0");
    assert(AdrenoSysfs::discover(platform).isEmpty());
    write(gpu + QStringLiteral("/of_node/compatible"), "qcom,adreno");
    devices = AdrenoSysfs::discover(platform);
    assert(devices.size() == 1 && devices[0].name == QStringLiteral("Adreno"));
    assert(QFile::remove(gpu + QStringLiteral("/gpu_busy_percent")));
    assert(!AdrenoSysfs::usage(gpu));
    assert(AdrenoSysfs::discover(platform).isEmpty());
    assert(AdrenoSysfs::discover(root + QStringLiteral("/absent")).isEmpty());
    std::cout << "PASS: Adreno discovery, idle/busy values, frequency, platform symlinks, deduplication, unsupported devices and invalid/missing readings\n";
}
