# Runbook Scout

Les commandes sont à exécuter depuis un checkout propre de `Tetrax/scout`. Les secrets et données restent sous le compte `tetrax` ; ne jamais les afficher, copier dans un ticket ou committer.

## 1. Bootstrap unique

```bash
umask 077
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.txt
sudo -u tetrax env PYTHONPATH="$PWD" "$PWD/.venv/bin/python" -m scout_web.credentials \
  --directory /home/tetrax/.config/scout \
  --url https://scout.valdev.me \
  --username valentin
sudo -u tetrax install -d -m 700 /home/tetrax/.local/state/scout/web/backups
```

La création refuse de remplacer `scout.env` ou `access.txt` existant. L’exécution sous `tetrax` garantit le propriétaire attendu sans correction de droits ultérieure.

Vérifier les métadonnées sans lire le contenu :

```bash
sudo stat -c '%U:%G %a %n' \
  /home/tetrax/.config/scout \
  /home/tetrax/.config/scout/scout.env \
  /home/tetrax/.config/scout/access.txt \
  /home/tetrax/.local/state/scout/web
```

La récupération ultérieure se fait par SSH avec le compte `tetrax`, en lisant `/home/tetrax/.config/scout/access.txt`. Aucun secret ne doit être transmis dans les logs ou le chat.

## 2. Variables de déploiement

Scout exige Docker Compose 2.30 ou plus récent : `compose.yaml` utilise
`env_file.format: raw` afin de transmettre les dérivés `scrypt` littéralement.
Ne pas remplacer ce format par la forme courte, car les caractères `$` d'un
dérivé seraient alors interprétés comme des variables Compose.

Créer `/home/tetrax/.config/scout/deployment.env` en mode 600 :

```dotenv
SCOUT_IMAGE=scout:<SHA_MAIN>
SCOUT_REVISION=<SHA_MAIN>
SCOUT_PORT=13739
SCOUT_UID=1000
SCOUT_GID=1000
SCOUT_DATA_DIR=/home/tetrax/.local/state/scout/web
SCOUT_ENV_FILE=/home/tetrax/.config/scout/scout.env
```

Remplacer `<SHA_MAIN>` par le SHA complet réellement fusionné. Ne pas utiliser `latest`.

## 3. Construire et vérifier une release

Construire depuis un worktree détaché du SHA fusionné :

```bash
release_sha="$(git rev-parse origin/main)"
worktree="/tmp/scout-release-$release_sha"
git worktree add --detach "$worktree" "$release_sha"
docker build \
  --build-arg APP_UID="$(id -u tetrax)" \
  --build-arg APP_GID="$(id -g tetrax)" \
  --build-arg SCOUT_REVISION="$release_sha" \
  --tag "scout:$release_sha" \
  "$worktree"
docker image inspect "scout:$release_sha" \
  --format 'id={{.Id}} user={{.Config.User}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
git worktree remove "$worktree"
```

Le résultat attendu est `user=scout:scout` et `revision=<SHA_MAIN>`.

## 4. Déployer

Avant mutation, noter l’image, l’ID, la santé et le nombre de redémarrages du conteneur courant. Si Scout existe déjà, conserver son image sous un tag de rollback immuable.

```bash
docker compose --env-file /home/tetrax/.config/scout/deployment.env config --quiet
docker compose --env-file /home/tetrax/.config/scout/deployment.env up -d --no-build
```

Gate obligatoire :

```bash
docker inspect scout-web \
  --format 'image={{.Config.Image}} id={{.Image}} state={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}}'
curl --fail --silent --show-error http://127.0.0.1:13739/healthz
```

Exiger `running`, `healthy`, `restarts=0` et la révision attendue avant de modifier Nginx.

## 5. Nginx et TLS

Le fichier versionné est `deploy/nginx/scout.valdev.me.conf`. L’installation réelle doit préserver une sauvegarde datée de tout fichier Scout remplacé, puis :

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Pour le premier certificat, utiliser le plugin Nginx de Certbot après mise en place du vhost HTTP exact :

```bash
sudo certbot --nginx -d scout.valdev.me
```

Puis vérifier :

```bash
curl --silent --show-error --head http://scout.valdev.me/
curl --fail --silent --show-error https://scout.valdev.me/robots.txt
openssl s_client -connect scout.valdev.me:443 -servername scout.valdev.me </dev/null
```

Le HTTP doit rediriger vers HTTPS ; le certificat doit porter `scout.valdev.me` ; `robots.txt` doit refuser tout crawl ; les réponses doivent porter `X-Robots-Tag: noindex, nofollow, noarchive`.

## 6. Collecte préparatoire

Après déploiement, préparer le cache réel sans créer de run et sans marquer d’item vu :

```bash
docker compose --env-file /home/tetrax/.config/scout/deployment.env exec -T web \
  python -m scripts.prefetch --database /var/lib/scout/scout.sqlite3
```

Le script affiche uniquement les statuts et nombres par source. Il ne crée ni réaction, ni favori, ni entrée d’historique.

## 7. Sauvegarde

Une sauvegarde cohérente peut être produite service actif :

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose --env-file /home/tetrax/.config/scout/deployment.env exec -T web \
  python -m scout_web.maintenance backup \
  --database /var/lib/scout/scout.sqlite3 \
  --output "/var/lib/scout/backups/scout-$stamp.sqlite3"
docker compose --env-file /home/tetrax/.config/scout/deployment.env exec -T web \
  python -m scout_web.maintenance verify \
  --database "/var/lib/scout/backups/scout-$stamp.sqlite3"
```

Les snapshots sont mode 600. Les copier hors du VPS selon la politique de sauvegarde personnelle si une résilience hôte est requise.

## 8. Restauration

1. Faire une sauvegarde de sécurité de la base courante.
2. Arrêter Scout uniquement.
3. Vérifier puis restaurer le snapshot.
4. Redémarrer et rejouer les smokes.

```bash
docker compose --env-file /home/tetrax/.config/scout/deployment.env stop web
docker compose --env-file /home/tetrax/.config/scout/deployment.env run --rm --no-deps web \
  python -m scout_web.maintenance restore \
  --backup /var/lib/scout/backups/<SNAPSHOT>.sqlite3 \
  --database /var/lib/scout/scout.sqlite3 \
  --service-stopped
docker compose --env-file /home/tetrax/.config/scout/deployment.env up -d --no-build
```

Ne jamais restaurer pendant que le service est actif.

## 9. Rollback applicatif

Le schéma 1 est la frontière de compatibilité de la release 1.0. Pour revenir à l’image précédente :

1. conserver d’abord une sauvegarde SQLite cohérente ;
2. remettre `SCOUT_IMAGE` et `SCOUT_REVISION` sur l’ancien SHA dans `deployment.env` ;
3. valider Compose ;
4. lancer `up -d --no-build` ;
5. exiger `healthy`, `restarts=0`, ancienne révision et parcours de connexion valide.

Si une future migration rend le schéma incompatible, restaurer le snapshot créé avant cette migration en suivant la section précédente.

## 10. Smokes de production

Après chaque livraison :

- backend loopback : `/healthz` retourne `status=ok` et le SHA exact ;
- HTTP externe redirige vers HTTPS ;
- HTTPS présente le bon certificat et les en-têtes de sécurité ;
- sans session, `/` redirige vers `/login` et `/api/status` répond 401 ;
- login réel, découverte, lien source, 👎/❤️/⭐, correction, favoris, historique et préférences fonctionnent ;
- redémarrage du conteneur conserve intérêts, réactions et favoris ;
- une reconstruction/recréation avec le même bind mount conserve les mêmes données ;
- console navigateur sans erreur et aucune overflow en 390 px et 1440 px ;
- les autres vhosts et conteneurs n’ont pas été recréés.
