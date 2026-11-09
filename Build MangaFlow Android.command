#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ANDROID_DIR="$ROOT/android"

if [[ -z "${JAVA_HOME:-}" ]]; then
  for candidate in /opt/homebrew/opt/openjdk@17 /usr/local/opt/openjdk@17; do
    if [[ -x "$candidate/bin/java" ]]; then
      export JAVA_HOME="$candidate"
      break
    fi
  done
fi

if [[ -z "${ANDROID_HOME:-}" ]]; then
  for candidate in "$HOME/Library/Android/sdk" /opt/homebrew/share/android-commandlinetools; do
    if [[ -d "$candidate/platforms/android-36" ]]; then
      export ANDROID_HOME="$candidate"
      break
    fi
  done
fi

if [[ -z "${JAVA_HOME:-}" || ! -x "$JAVA_HOME/bin/java" ]]; then
  echo "JDK 17 is required. Install it with: brew install openjdk@17"
  read -r -p "Press Enter to close..."
  exit 1
fi

if [[ -z "${ANDROID_HOME:-}" ]]; then
  echo "Android SDK 36 was not found. Install Android Studio and Android SDK Platform 36."
  read -r -p "Press Enter to close..."
  exit 1
fi

export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$PATH"
cd "$ANDROID_DIR"
./gradlew assembleDebug

APK="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
echo
echo "MangaFlow APK is ready: $APK"
open -R "$APK"
read -r -p "Press Enter to close..."
