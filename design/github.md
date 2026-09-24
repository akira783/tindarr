repo: akira783/tindarr
branch: main
path: web/

## Last sync
date: 2026-09-24T20:09:28Z

### Updated in this project
- Nouveaux jetons CSS, thèmes sombre et clair (tokens.css)
- Spécifications de la carte, de la barre de verdicts, des badges, de l'attente et des impasses
- Planches du deck : bureau et téléphone, sombre et clair, plus l'état d'attente

## Screen map
| Screen | Repo files |
|---|---|
| Tindarr Refonte.dc.html — jetons | web/src/styles/index.css |
| Tindarr Refonte.dc.html — carte, badges | web/src/features/deck/DeckCard.tsx, api/openapi.yaml (Card) |
| Tindarr Refonte.dc.html — verdicts | web/src/features/deck/DeckPage.tsx, web/src/features/deck/keys.ts |
| Tindarr Refonte.dc.html — réglages du deck | web/src/features/deck/DeckControls.tsx |
| Tindarr Refonte.dc.html — attente, impasses | web/src/features/deck/DeckPage.tsx, shared/i18n/fr/console.json |
| Tindarr Refonte.dc.html — shell | web/src/components/Layout.tsx, web/src/components/ui.tsx |
| tokens.css | web/src/styles/index.css, docs/adr/0009-web-console-and-phone-pairing.md |
