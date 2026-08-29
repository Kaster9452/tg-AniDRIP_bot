# 🚀 Guidebook: how to deploy this bot for yourself

This bot is a **universal "source"**: you can deploy it for your own
channel, your own admin group and your own database. Everything that
needs to be configured lives in environment variables — you won't have
to change any code.

Below is a step-by-step walkthrough "from zero to a working bot".
Time: ~30 minutes.

---

## What you need in advance

- **A Telegram account** (obviously 🙂)
- **A GitHub account** — to copy the project and connect deployment
- **A Render account** — free hosting where the bot will live
  ([render.com](https://render.com))
- **A Neon account** — free PostgreSQL database
  ([neon.tech](https://neon.tech)) — or any other PostgreSQL
- **A cron-job.org account** — a free "alarm clock" so the bot doesn't
  fall asleep on Render's free tier ([cron-job.org](https://cron-job.org))

---

## Step 1. Create the bot in @BotFather

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, pick a name and a username (the username must end
   with `bot`, e.g. `my_channel_bot`).
3. BotFather will send you a **token** like `123456789:AAH...`.
   Copy it — you'll need it later.
4. (Optional) set a description and an avatar for the bot.

## Step 2. Create the admin group

The bot forwards submissions to a group where moderators decide what
gets published.

1. Create a new Telegram group (e.g. "Channel Admins").
2. **Add the bot to the group** and make it an **administrator**
   (group info → "Add member" → pick the bot; in the member list give
   it admin rights). Without this Telegram won't let the bot read all
   group messages.
3. Get the group ID: add the `@RawDataBot` bot to the group
   (or any other group-ID bot) and send any message in the group — it
   will reply with data that includes the group ID
   (`chat id: ...`, looks like `-100...`). If you don't have such a
   bot, forward a message from this group to `@RawDataBot` — it will
   show the original chat's ID as well. After that you can remove
   `@RawDataBot` from the group. Write the ID down (your
   `ADMIN_GROUP_ID`).

> Once the bot is already running (after Step 6), there is an easier
> way to get the group ID: send the `/id` command in the group — the
> bot itself will reply with the number.

## Step 3. Create the channel

This is where the bot will publish approved posts.

1. Create a channel (private or public — doesn't matter).
2. Add the bot as an **administrator** with the **"Post messages"**
   permission (channel info → "Administrators" → "Add administrator" →
   the bot → enable the posting permission).
3. Get the channel ID: if the channel is public, the easiest way is
   via `@RawDataBot` — forward a message from the channel to it and it
   will show an ID like `-100...`. Add the `-100` prefix manually if
   it's missing. Write this number down (your `CHANNEL_ID`).

## Step 4. Create the database (Neon)

The bot keeps permanent per-author stats and the queue — everything is
stored in PostgreSQL.

1. Sign up at [neon.tech](https://neon.tech) (there is a free tier and
   the database never expires).
2. Create a new project → you'll get a connection string like
   `postgresql://user:pass@host/dbname?sslmode=require`.
3. Copy the whole string (your `DATABASE_URL`).

> The bot creates all tables automatically on first launch — there is
> nothing to set up manually.

## Step 5. (Optional) Register the Mini App — the moderation panel

The moderation panel (a web UI for admins) opens from the bot. For it
to work, you need to register the Mini App once:

1. Open [@BotFather](https://t.me/BotFather), send `/newapp`, choose
   your bot.
2. Enter a name and a description, attach a 640×360 image
   (you can skip the animation with the `/empty` command).
3. In the app URL field enter
   `https://your-service.onrender.com/app` later (after Step 6).
4. Come up with a short name, e.g. `panel` — that's your
   `WEBAPP_SHORT_NAME`.

> Even if you don't register the Mini App, the panel still works:
> admins open it with the `/panel` command — both in private chat and
> in the group. Registering the Mini App adds a separate panel button
> to your private chat with the bot.

## Step 6. Copy the project and deploy it to Render

1. **Fork** the source repository (the Fork button on GitHub) — this
   gives you your own copy you can commit to.
2. Go to [render.com](https://render.com) → **New → Web Service**.
3. Connect your GitHub and pick the fork of the project.
4. Fill in:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python -m bot.main`
   - **Environment** — add the variables from the step below.
5. Click **Create Web Service**. Render will install the dependencies,
   start the bot and show an address like
   `https://your-service.onrender.com`.

## Step 7. Environment variables (the main "make it yours" part)

In **Render → Environment** add:

| Variable | Required | What to put in |
|---|---|---|
| `BOT_TOKEN` | ✅ | the token from Step 1 |
| `ADMIN_GROUP_ID` | ✅ | the admin group ID from Step 2 |
| `CHANNEL_ID` | ✅ | the channel ID from Step 3 |
| `DATABASE_URL` | ✅ | the Neon string from Step 4 |
| `TIMEZONE` | — | `Europe/Moscow` (default) |
| `WEBAPP_URL` | for the panel | `https://your-service.onrender.com` |
| `WEBAPP_SHORT_NAME` | for the panel | `panel` (from Step 5) |
| `BOT_NAME` | — | the bot's name shown in the panel and help, e.g. `MyChannel` |
| `PORT` | ❌ | Render sets it itself, don't touch |

> The same set is described in the **`.env.example`** file in the
> project root.

After adding the variables **restart** the service (Render restarts
automatically when Environment changes).

## Step 8. Verification (checklist)

1. Send the bot any text in a private chat — you should get a greeting
   and the submission should appear in the admin group.
2. In the admin group press **📢 Опубликовать (Publish)** → pick a mode —
   the post should go to the channel.
3. Press **🕓 Отложить (Postpone)** — pick a time — the author should get
   a "post postponed" notification.
4. Open the panel: `/panel` in a private chat with the bot (or the
   "Open panel" button) — the panel should load with your name from
   `BOT_NAME`.

> The bot's interface is in Russian; the original button labels are
> kept above so you can find them.

## Step 9. Keep the bot awake (cron-job.org)

Free Render puts the service to sleep if nobody contacts it for about
15 minutes: the bot stops responding until someone opens its address.
The fix is a free timer:

1. Sign up at [cron-job.org](https://cron-job.org).
2. Create a job (Cronjob): URL — `https://your-service.onrender.com/`
   (the same address from Step 6), schedule — **every 10 minutes**.
3. Enable the job (the Start button).

Now the service will wake up on its own and the bot will answer around
the clock.

## How to update the bot (get new versions)

The project evolves and new features appear in the source repository.
To bring them into your fork:

1. Open your fork on GitHub.
2. When there is something new upstream, a **Sync fork** button appears
   at the top → click it → **Update branch**.
3. Render will restart the bot after the update.

Your settings (the environment variables on Render) are not touched —
they live in the Render dashboard, not in the code.

## Good to know

**The bot token is a password.** Never publish it anywhere. If the
token ever leaks: @BotFather → `/mybots` → your bot → API Token →
**Revoke current token** (the old one stops working; paste the new one
into Render).

**The queue is never lost.** Scheduled posts and per-author stats live
in the database — a Render restart can't hurt them. The only thing
that disappears on a restart is a half-entered schedule time: just
press "Postpone" again.

**A slot every three hours.** A scheduled post takes the nearest free
slot in the posting grid. You can write your own post with the
`/mypost` command in the admin group — the bot will offer to pick a
slot.

---

## Running locally (for development)

1. Install Python 3.11+ and `git`.
2. Clone the project: `git clone <your-fork-url>`
3. `cd <folder>`
4. Create a virtual environment and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # Linux/macOS
   pip install -r requirements.txt
   ```
5. Copy `.env.example` to `.env` and fill in the values.
6. Run: `python -m bot.main`

The bot will start locally and begin listening to Telegram
(long polling).

## Running the tests

```bash
python -m compileall -q bot tests
python -m unittest discover -s tests -v
```

All 38 tests must pass without errors.

---

## 🛠 Troubleshooting

**The bot doesn't see messages in the group.**
→ The bot is not a group administrator. Without admin rights Telegram
only delivers messages addressed to the bot directly.

**The post isn't published to the channel.**
→ The bot is not a channel administrator, or it lacks the
"Post messages" permission. Check the permissions in the channel info.

**Database connection error.**
→ Wrong `DATABASE_URL`, or the database refuses connections. On startup
the bot checks the connection and logs the error in the Render log
(Logs tab).

**The panel doesn't open from the group.**
→ The Mini App isn't registered (Step 5) or `WEBAPP_SHORT_NAME` is
wrong. The easiest way is to open the panel from a private chat with
the bot — that works right away.

**The bot was answering, then stopped.**
→ The service fell asleep on free Render — nobody contacted it for 15
minutes. Set up cron-job.org (Step 9).

**The postpone time was lost.**
→ The service restarted, and a half-entered time isn't saved — pick
"Postpone" again. Already scheduled posts are never lost.

**The author doesn't get notifications.**
→ Usually it means the author blocked the bot. This is not an error:
the bot keeps publishing and logs that the message wasn't delivered.

---

## Quick checklist of all values

```
BOT_TOKEN           = <from @BotFather>
ADMIN_GROUP_ID      = <admin group ID, -100...>
CHANNEL_ID          = <channel ID, -100...>
DATABASE_URL        = <Neon/Supabase postgres://...>
TIMEZONE            = Europe/Moscow
WEBAPP_URL          = https://your-service.onrender.com
WEBAPP_SHORT_NAME   = panel
BOT_NAME            = MyChannel
```
