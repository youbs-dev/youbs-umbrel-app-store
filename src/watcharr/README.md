# Watcharr (correctifs youbs)

Image Watcharr officielle à laquelle on applique les correctifs de `patches/`.
Le workflow `Image watcharr` construit `ghcr.io/youbs-dev/watcharr:<version>`, où
`<version>` est celle de `youbs-watcharr/umbrel-app.yml`.

- `UPSTREAM` : tag de Watcharr utilisé (par exemple `v4.3.0`).
- `patches/0001-tmdb-langue-metadonnees.patch` : la variable d'environnement `TMDB_LANG`
  (par exemple `fr-FR`) choisit la langue des titres et résumés TMDB. Les champs non
  traduits sont complétés en anglais, et les bandes-annonces anglaises restent visibles.
  Sans `TMDB_LANG`, Watcharr reste en anglais comme l'original.

À retirer quand Watcharr gérera la langue lui-même (voir sbondCo/Watcharr#1079).

## Passer à une nouvelle version de Watcharr

1. Mettre le nouveau tag dans `UPSTREAM`.
2. Vérifier que le correctif s'applique toujours :
   `git clone --depth 1 --branch <tag> https://github.com/sbondCo/Watcharr && cd Watcharr && git apply --check ../patches/*.patch`
3. Mettre la version dans `youbs-watcharr/umbrel-app.yml` (`<tag sans v>.1`) et le même tag
   d'image dans `youbs-watcharr/docker-compose.yml`.
