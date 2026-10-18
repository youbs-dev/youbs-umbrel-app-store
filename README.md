# youbs's app — Umbrel Community App Store

App store communautaire pour umbrelOS, pour installer des applications absentes du store officiel.

## Applications

| App | ID | Version |
| --- | --- | --- |
| Watcharr | `youbs-watcharr` | 4.2.2 |
| Dawarich | `youbs-dawarich` | 1.15.0.2 |
| Bambuddy | `youbs-bambuddy` | 1.2.5.7 |
| Rclone Seedbox | `youbs-rclone` | 1.75.1.3 |
| wger | `youbs-wger` | 2.7 |
| Lab Inventory | `youbs-lab-inventory` | 0.9.0 |
| Headscale (+ Headplane) | `youbs-headscale` | 0.29.4 |
| Tailscale (Headscale) | `youbs-tailscale` | 1.102.5 |
| Pi-hole (eno1) | `youbs-pihole` | 2026.09.0.1 |
| Radarr | `youbs-radarr` | 6.4.4.10685-1 |
| Sonarr | `youbs-sonarr` | 4.0.20.3014-1 |
| Lidarr | `youbs-lidarr` | 3.1.0.4875-1 |
| Prowlarr | `youbs-prowlarr` | 2.6.5.5623-1 |
| Carbure | `youbs-carbure` | 1.2.0 |
| Suivi de prix | `youbs-suivi-prix` | 0.4.0 |
| MoneyMatter | `youbs-moneymatter` | f27fc49 |
| OpenTAKServer | `youbs-opentakserver` | 1.7.13-1 |
| Notesnook (serveur de synchro) | `youbs-notesnook` | 1.0-beta.38 |

## Ajouter ce store dans Umbrel

Dans umbrelOS : App Store > menu `...` > Community App Stores, puis ajouter :

```
https://github.com/youbs-dev/youbs-umbrel-app-store
```

## Ajouter une application

1. Créer un dossier `youbs-<nom-app>/` (l'ID de l'app doit commencer par `youbs-`).
2. Y placer `umbrel-app.yml` (fiche de l'app) et `docker-compose.yml`.
3. Dans le compose, `app_proxy.environment.APP_HOST` doit valoir `<id-app>_<service>_1` et `APP_PORT` le port interne du conteneur.
4. Stocker les données sous `${APP_DATA_DIR}` et dériver les secrets de `${APP_SEED}`.
5. Épingler les images sur une version précise (pas de `:latest`).

## Applications développées ici

Le code source des apps Lab Inventory et Suivi de prix se trouve dans `src/lab-inventory/` et `src/suivi-prix/`. À chaque modification
sur `main`, le workflow `Image lab-inventory` construit l'image `ghcr.io/youbs-dev/lab-inventory`
avec le tag de la version indiquée dans `youbs-lab-inventory/umbrel-app.yml`. Pour publier une nouvelle
version : modifier le code, augmenter `version` dans `umbrel-app.yml` et le tag de l'image dans `docker-compose.yml`.

Suivi de prix suit le même principe : le workflow `Image suivi-prix` construit `ghcr.io/youbs-dev/suivi-prix`
avec le tag de la version indiquée dans `youbs-suivi-prix/umbrel-app.yml`.
