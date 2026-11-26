# Set up on another computer with Docker (with your data)

For **Windows** with Docker Desktop and Git, on a computer that has **never run this Docker project**.
Run the commands in PowerShell, **one line at a time**, and stop if one fails.

## Before you start

- The latest application code must be on GitHub (not only this file).
- Copy these two backup files to `E:\wargame-backup` on the new computer. **They are not on GitHub**, and should
  stay private because the database can include account details.
  - `wargame.dump` (database)
  - `vectors.zip` (map files and tiles)

## 1. Get the code

```powershell
git clone https://github.com/Manjudaksh/Wargame.git
cd Wargame
```

## 2. Change the database version

The backup was made with PostgreSQL 17, and PostgreSQL 16 cannot read it. In `docker-compose.yml`, change
`image: postgres:16-alpine` to:

```yaml
image: postgres:17-alpine
```

Make this change **only on the new computer, and do not push it**. An existing PostgreSQL 16 Docker database cannot be
opened directly by version 17.

## 3. Restore the database (before starting the app)

```powershell
docker compose up -d --wait db
docker compose cp E:\wargame-backup\wargame.dump db:/tmp/wargame.dump
docker compose exec db pg_restore -U wargame -d wargame --no-owner --exit-on-error /tmp/wargame.dump
```

## 4. Start the app

```powershell
docker compose up -d --build
```

## 5. Add the map files

```powershell
Expand-Archive -Path E:\wargame-backup\vectors.zip -DestinationPath E:\wargame-restore
docker compose cp E:\wargame-restore\vectors web:/data/
```

Open <http://localhost:8000> and sign in with your existing app login (it comes from the restored database).

## Important

- **Never run `docker compose down -v`.** It deletes the Docker database and stored files.
- The Docker settings use a simple database password. Do not expose the app to the internet without securing it first.
- No `.env` file is needed for the database with this Docker setup.
- This complete flow has not yet been tested from start to finish.
