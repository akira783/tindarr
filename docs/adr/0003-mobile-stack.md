# 0003. Mobile stack: Expo (React Native), Android first

- Status: accepted
- Date: 2026-09-22
- Amended by: [0009](0009-web-console-and-phone-pairing.md)

> **Note (2026-09-22):** the app no longer has admin screens; setup and admin moved
> to a web console (React, Vite, TypeScript) that shares the generated client and the
> translations. See ADR 0009. The stack below is unchanged.

## Context

Android first, with an iOS port if the app finds an audience. Swipe gestures must feel
native. The existing web UI is Vue/JS.

## Decision

- Expo (React Native) with TypeScript `strict`, continuous native generation (no
  hand-edited `android/` folder).
- Gestures and animation with `react-native-gesture-handler` and
  `react-native-reanimated`. Images with `expo-image` (disk cache, prefetch). Haptics
  with `expo-haptics`.
- TanStack Query for server state and Zustand for local state. Translations with
  i18next (en, fr).
- API client generated from the OpenAPI contract.
- Tests: Jest + React Native Testing Library for logic and components, Maestro for
  end-to-end flows on an emulator in CI.
- Builds: local Gradle builds in CI (reproducible, free) produce signed APK/AAB.
  EAS Build is used later for iOS without a Mac.
- No Android-only native module without an iOS equivalent.

## Consequences

- One codebase serves the future iOS port, which is mostly a build, store and
  notification setup.
- The deck logic already written in JS (`swipeDeck.js`, `swipeProfile.js`) can be
  ported to TypeScript with its tests.

## Alternatives considered

- **Flutter.** Good performance, but Dart is new to the project and nothing could be
  reused. Rejected.
- **Native Kotlin.** Best Android experience, but a full rewrite for iOS later.
  Rejected.
- **PWA.** Weaker gestures and notifications on iOS, no store presence. Rejected.
