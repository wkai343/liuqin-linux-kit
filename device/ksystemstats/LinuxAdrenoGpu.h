// SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
#pragma once

#include "AdrenoSysfs.h"
#include "GpuDevice.h"

class LinuxAdrenoGpu : public GpuDevice
{
public:
    LinuxAdrenoGpu(const QString &id, const AdrenoSysfs::Device &device);
    void initialize() override;
    void update() override;

private:
    AdrenoSysfs::Device m_device;
};
