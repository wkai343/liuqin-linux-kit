// SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
#include "LinuxAdrenoGpu.h"

#include <KLocalizedString>

LinuxAdrenoGpu::LinuxAdrenoGpu(const QString &id, const AdrenoSysfs::Device &device)
    : GpuDevice(id, device.name)
    , m_device(device)
{
}

void LinuxAdrenoGpu::initialize()
{
    // Adreno shares system RAM. Publish only measured values, without fake
    // dedicated VRAM, temperature or power readings from another device.
    m_nameProperty = new KSysGuard::SensorProperty(QStringLiteral("name"), this);
    m_nameProperty->setName(i18nc("@title", "Name"));
    m_nameProperty->setPrefix(name());
    m_nameProperty->setValue(name());
    m_usageProperty = new KSysGuard::SensorProperty(QStringLiteral("usage"), QStringLiteral("usage"), 0, this);
    m_usageProperty->setName(i18nc("@title", "Usage"));
    m_usageProperty->setPrefix(name());
    m_usageProperty->setUnit(KSysGuard::UnitPercent);
    m_usageProperty->setMin(0);
    m_usageProperty->setMax(100);
    if (!m_device.frequencyPath.isEmpty()) {
        m_coreFrequencyProperty = new KSysGuard::SensorProperty(QStringLiteral("coreFrequency"), QStringLiteral("coreFrequency"), 0, this);
        m_coreFrequencyProperty->setName(i18nc("@title", "Frequency"));
        m_coreFrequencyProperty->setPrefix(name());
        m_coreFrequencyProperty->setUnit(KSysGuard::UnitMegaHertz);
    }
    update();
}

void LinuxAdrenoGpu::update()
{
    const auto busy = AdrenoSysfs::usage(m_device.path);
    m_usageProperty->setValue(busy ? QVariant::fromValue(*busy) : QVariant{});
    if (m_coreFrequencyProperty) {
        const auto hz = AdrenoSysfs::number(m_device.frequencyPath);
        m_coreFrequencyProperty->setValue(hz ? QVariant::fromValue(*hz / 1000000.0) : QVariant{});
    }
}
