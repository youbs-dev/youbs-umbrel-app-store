# youbs's app — Umbrel Community App Store

App store communautaire pour umbrelOS, pour installer des applications absentes du store officiel.

## Applications

| App | ID | Version |
| --- | --- | --- |
| Watcharr | `youbs-watcharr` | 4.2.1 |
| Dawarich | `youbs-dawarich` | 1.15.0 |
| Bambuddy | `youbs-bambuddy` | 1.2.5.5 |
| Rclone Seedbox | `youbs-rclone` | 1.75.1.2 |

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
