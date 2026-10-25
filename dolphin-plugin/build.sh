#!/bin/bash
# Build the Dolphin indicator plugin without cmake/ECM: g++ + moc only.
# Installs into <builddir>/dolphin/vcs/ so it can be tried with QT_PLUGIN_PATH.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SRC}/build"
MOC=/usr/lib/qt6/moc

QTINC=/usr/include/qt6
INC="-I${QTINC} -I${QTINC}/QtCore -I${QTINC}/QtGui -I${QTINC}/QtWidgets"
INC+=" -I/usr/include/KF6 -I/usr/include/KF6/KIOCore -I/usr/include/KF6/KCoreAddons"
INC+=" -I/usr/include/KF6/KIO -I/usr/include/KF6/KIOWidgets -I/usr/include/KF6/KIOGui"
INC+=" -I/usr/include/KF6/KI18n -I/usr/include/Dolphin"
INC+=" -I/usr/lib/qt6/mkspecs/linux-g++"

mkdir -p "${OUT}"

echo "== moc =="
"${MOC}" "${SRC}/yadisk_vcs.cpp" -o "${OUT}/yadisk_vcs.moc" -I"${SRC}" ${INC}

echo "== compile =="
g++ -std=c++20 -O2 -fPIC -shared -Wall -Wextra -o "${OUT}/yadisk_vcs.so" \
    "${SRC}/yadisk_vcs.cpp" \
    ${INC} -I"${OUT}" \
    -lKF6CoreAddons -lKF6KIOCore -lKF6I18n -ldolphinvcs -lQt6Core -lQt6Gui -lQt6Widgets

echo "== install layout =="
rm -rf "${OUT}/dolphin"
mkdir -p "${OUT}/dolphin/vcs"
cp "${OUT}/yadisk_vcs.so" "${OUT}/dolphin/vcs/"
ls -la "${OUT}/dolphin/vcs/"
echo "built: ${OUT}/yadisk_vcs.so"
