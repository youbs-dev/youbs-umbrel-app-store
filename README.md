# youbs's app — Umbrel Community App Store

App store communautaire pour umbrelOS, pour installer des applications absentes du store officiel.

## Applications

| App | ID | Version |
| --- | --- | --- |
| Watcharr | `youbs-watcharr` | 4.2.2 |
| Dawarich | `youbs-dawarich` | 1.15.0.2 |
| Bambuddy | `youbs-bambuddy` | 1.2.5.5 |
| Rclone Seedbox | `youbs-rclone` | 1.75.1.3 |
| wger | `youbs-wger` | 2.7 |
| Inventaire composants | `youbs-inventaire` | 0.1.0 |

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

Le code source de l'app Inventaire composants se trouve dans `src/inventaire/`. À chaque modification
sur `main`, le workflow `Image inventaire` construit l'image `ghcr.io/youbs-dev/inventaire-composants`
avec le tag de la version indiquée dans `youbs-inventaire/umbrel-app.yml`. Pour publier une nouvelle
version : modifier le code, augmenter `version` dans `umbrel-app.yml` et le tag de l'image dans `docker-compose.yml`.
