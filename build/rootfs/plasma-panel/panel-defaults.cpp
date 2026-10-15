// SPDX-License-Identifier: MIT
// Set the internal tablet panel's initial rotation policy once per user.
// Later choices made in Display Configuration remain the user's choices.
#include <QGuiApplication>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QStandardPaths>
#include <QTimer>
#include <KScreen/Config>
#include <KScreen/GetConfigOperation>
#include <KScreen/SetConfigOperation>
#include <KScreen/Output>

int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);
    const QString marker = QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation)
        + QStringLiteral("/liuqin-panel-initialized");
    if (QFile::exists(marker)) return 0;
    auto *read = new KScreen::GetConfigOperation();
    QObject::connect(read, &KScreen::GetConfigOperation::finished, &app, [&app, marker](KScreen::ConfigOperation *op) {
        auto *get = static_cast<KScreen::GetConfigOperation *>(op);
        if (get->hasError() || !get->config()) { app.exit(1); return; }
        bool found = false;
        for (const auto &output : get->config()->outputs()) {
            if (output->name() != QStringLiteral("DSI-1")) continue;
            output->setAutoRotatePolicy(KScreen::Output::AutoRotatePolicy::Always);
            found = true;
        }
        if (!found) { app.exit(1); return; }
        auto *write = new KScreen::SetConfigOperation(get->config());
        QObject::connect(write, &KScreen::SetConfigOperation::finished, &app, [&app, marker](KScreen::ConfigOperation *result) {
            if (result->hasError()) { app.exit(1); return; }
            QDir().mkpath(QFileInfo(marker).absolutePath());
            QFile file(marker);
            const bool ok = file.open(QIODevice::WriteOnly) && file.write("rotation-default-v1\n") > 0;
            app.exit(ok ? 0 : 1);
        });
    });
    QTimer::singleShot(20000, &app, [&app]() { app.exit(1); });
    return app.exec();
}
