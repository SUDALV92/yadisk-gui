/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Dolphin indicator plugin for the Yandex Disk shadow-file app.
 *
 * Dolphin's KOverlayIconPlugin API is not consulted by KIO/KFileItem on
 * KF6/Plasma 6, so the state is rendered through the version-control plugin
 * interface instead (the same trick Insync uses): every item inside the mount
 * gets an "ItemVersion" which Dolphin paints as an emblem in the top-left
 * corner of the icon.
 *
 *   available online only -> NormalVersion          "vcs-normal"           (blue cloud)
 *   kept on this device   -> LocallyModifiedVersion "vcs-locally-modified" (green check)
 *
 * Two states only, like the Windows client.  Bytes that happen to sit in the
 * rclone LRU cache are an implementation detail, not a state, so such files
 * still report "cloud".
 *
 * The state itself comes from two tiny text files written by
 * `yadisk state`; itemVersion() only does hash lookups, so it never blocks.
 */

#include <KFileItem>
#include <KLocalizedString>
#include <KPluginFactory>
#include <KVersionControlPlugin>

#include <QAction>
#include <QDateTime>
#include <QIcon>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QSet>
#include <QStandardPaths>
#include <QStringList>
#include <QTextStream>
#include <QUrl>

namespace
{
QStringList readLines(const QString &path)
{
    QStringList out;
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return out;
    }
    QTextStream in(&f);
    in.setEncoding(QStringConverter::Utf8);
    while (!in.atEnd()) {
        const QString line = in.readLine().trimmed();
        if (!line.isEmpty()) {
            out.append(line);
        }
    }
    return out;
}
} // namespace

class YadiskVcsPlugin : public KVersionControlPlugin
{
    Q_OBJECT

public:
    explicit YadiskVcsPlugin(QObject *parent, const QVariantList &args)
        : KVersionControlPlugin(parent)
    {
        Q_UNUSED(args)
        m_stateDir = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
            + QStringLiteral("/yadisk/state");

        m_actPin = new QAction(QIcon::fromTheme(QStringLiteral("cloud-download")), i18n("Keep on this device"), this);
        connect(m_actPin, &QAction::triggered, this, &YadiskVcsPlugin::pinTargets);

        m_actFree = new QAction(QIcon::fromTheme(QStringLiteral("edit-delete-shred")), i18n("Available online only"), this);
        connect(m_actFree, &QAction::triggered, this, &YadiskVcsPlugin::freeTargets);

        m_actFetch = new QAction(QIcon::fromTheme(QStringLiteral("download")), i18n("Download Now (do not keep)"), this);
        connect(m_actFetch, &QAction::triggered, this, &YadiskVcsPlugin::fetchTargets);

        m_actApp = new QAction(QIcon::fromTheme(QStringLiteral("folder-cloud")), i18n("Yandex Disk Settings…"), this);
        connect(m_actApp, &QAction::triggered, this, &YadiskVcsPlugin::openApp);

        reloadState(true);
    }

    QString fileName() const override
    {
        return QStringLiteral(".yadisk");
    }

    QString localRepositoryRoot(const QString &directory) const override
    {
        if (m_mount.isEmpty()) {
            return QString();
        }
        const QString dir = QDir::cleanPath(directory);
        if (dir == m_mount || dir.startsWith(m_mount + QLatin1Char('/'))) {
            return m_mount;
        }
        return QString();
    }

    bool beginRetrieval(const QString &directory) override
    {
        Q_UNUSED(directory)
        reloadState(false);
        return true;
    }

    void endRetrieval() override
    {
    }

    ItemVersion itemVersion(const KFileItem &item) const override
    {
        const QString rel = relPath(item.url());
        if (rel.isEmpty()) {
            return UnversionedVersion;
        }
        const bool pinned = isPinned(rel);
        if (item.isDir()) {
            // Mirrors the Windows client: a folder is only "kept" when it has
            // been pinned, otherwise its contents are considered online-only.
            return pinned ? LocallyModifiedVersion : NormalVersion;
        }
        // Two states only, like the Windows client: kept on this device
        // (green check) or living in the cloud (blue cloud). Bytes that happen
        // to sit in the LRU cache are an implementation detail of the mount,
        // not a user-visible state, so they still read as "cloud".
        if (pinned) {
            return LocallyModifiedVersion;
        }
        return NormalVersion;
    }

    QList<QAction *> versionControlActions(const KFileItemList &items) const override
    {
        m_targetRels.clear();
        for (const KFileItem &item : items) {
            const QString rel = relPath(item.url());
            if (!rel.isEmpty()) {
                m_targetRels.append(rel);
            }
        }
        if (m_targetRels.isEmpty()) {
            return {};
        }
        return {m_actPin, m_actFetch, m_actFree, m_actApp};
    }

    QList<QAction *> outOfVersionControlActions(const KFileItemList &items) const override
    {
        Q_UNUSED(items)
        return {};
    }

private:
    QString relPath(const QUrl &url) const
    {
        if (m_mount.isEmpty() || !url.isLocalFile()) {
            return QString();
        }
        const QString path = QDir::cleanPath(url.toLocalFile());
        if (path == m_mount || !path.startsWith(m_mount + QLatin1Char('/'))) {
            return QString();
        }
        return path.mid(m_mount.size() + 1);
    }

    bool isPinned(const QString &rel) const
    {
        if (m_pins.contains(rel)) {
            return true;
        }
        for (const QString &pin : m_pinList) {
            if (rel.startsWith(pin + QLatin1Char('/'))) {
                return true;
            }
        }
        return false;
    }

    void loadMountPoint()
    {
        const QStringList fromFile = readLines(m_stateDir + QStringLiteral("/mount.txt"));
        if (!fromFile.isEmpty()) {
            m_mount = QDir::cleanPath(fromFile.first());
            return;
        }
        // Fallback: read the app config directly.
        const QString cfgPath = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)
            + QStringLiteral("/yadisk/config.json");
        QFile f(cfgPath);
        if (f.open(QIODevice::ReadOnly)) {
            const QJsonObject cfg = QJsonDocument::fromJson(f.readAll()).object();
            QString mp = cfg.value(QStringLiteral("mountpoint")).toString();
            if (mp.startsWith(QLatin1Char('~'))) {
                mp.replace(0, 1, QDir::homePath());
            }
            if (!mp.isEmpty()) {
                m_mount = QDir::cleanPath(mp);
            }
        }
    }

    void reloadState(bool force)
    {
        loadMountPoint();

        const QString localPath = m_stateDir + QStringLiteral("/local.txt");
        const QString pinsPath = m_stateDir + QStringLiteral("/pins.txt");
        const QDateTime localMtime = QFileInfo(localPath).lastModified();
        const QDateTime pinsMtime = QFileInfo(pinsPath).lastModified();

        if (!force && localMtime == m_localMtime && pinsMtime == m_pinsMtime) {
            return;
        }
        m_localMtime = localMtime;
        m_pinsMtime = pinsMtime;

        const QStringList localList = readLines(localPath);
        m_local = QSet<QString>(localList.cbegin(), localList.cend());
        m_pinList = readLines(pinsPath);
        m_pins.clear();
        for (const QString &p : std::as_const(m_pinList)) {
            m_pins.insert(p);
        }
    }

    void runCli(const QStringList &args)
    {
        const QString bin = QStandardPaths::findExecutable(QStringLiteral("yadisk"),
                                                          {QDir::homePath() + QStringLiteral("/.local/bin")});
        if (bin.isEmpty()) {
            Q_EMIT errorMessage(i18n("The yadisk command was not found."));
            return;
        }
        auto *proc = new QProcess(this);
        proc->setProgram(bin);
        proc->setArguments(args);
        connect(proc, &QProcess::finished, this, [this, proc, args](int code, QProcess::ExitStatus status) {
            proc->deleteLater();
            const QString err = QString::fromUtf8(proc->readAllStandardError()).trimmed();
            if (code != 0 || status != QProcess::NormalExit) {
                Q_EMIT errorMessage(err.isEmpty() ? i18n("Command failed: %1", args.join(QLatin1Char(' '))) : err);
            } else {
                Q_EMIT operationCompletedMessage(QString());
            }
            reloadState(true);
            Q_EMIT itemVersionsChanged();
        });
        proc->start();
    }

    void pinTargets()
    {
        if (m_targetRels.isEmpty()) {
            return;
        }
        runCli(QStringList{QStringLiteral("pin")} + m_targetRels);
    }

    void freeTargets()
    {
        if (m_targetRels.isEmpty()) {
            return;
        }
        runCli(QStringList{QStringLiteral("evict")} + m_targetRels);
    }

    void fetchTargets()
    {
        if (m_targetRels.isEmpty()) {
            return;
        }
        runCli(QStringList{QStringLiteral("hydrate")} + m_targetRels);
    }

    void openApp()
    {
        const QString bin = QStandardPaths::findExecutable(QStringLiteral("yadisk"),
                                                          {QDir::homePath() + QStringLiteral("/.local/bin")});
        if (!bin.isEmpty()) {
            QProcess::startDetached(bin, {QStringLiteral("gui")});
        }
    }

    QString m_stateDir;
    QString m_mount;
    QSet<QString> m_local;
    QSet<QString> m_pins;
    QStringList m_pinList;
    QDateTime m_localMtime;
    QDateTime m_pinsMtime;

    mutable QStringList m_targetRels;
    QAction *m_actPin = nullptr;
    QAction *m_actFree = nullptr;
    QAction *m_actFetch = nullptr;
    QAction *m_actApp = nullptr;
};

K_PLUGIN_CLASS_WITH_JSON(YadiskVcsPlugin, "yadisk_vcs.json")

#include "yadisk_vcs.moc"
