import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from functools import partial

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web
from github import Github, GithubException, Auth as GithubAuth

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_REPO     = os.environ["GITHUB_REPO"]
GITHUB_FILE_PATH = os.environ["GITHUB_FILE_PATH"]
PORT = int(os.environ.get("PORT", "9000"))

# Intervalo em minutos para rodar a limpeza automática
CLEANUP_INTERVAL_MINUTES = int(os.environ.get("CLEANUP_INTERVAL_MINUTES", "30"))

BRASILIA_TZ = timezone(timedelta(hours=-3))

def now_br() -> str:
    return datetime.now(BRASILIA_TZ).strftime("%Y-%m-%dT%H:%M:%S-03:00")

gh = Github(auth=GithubAuth.Token(GITHUB_TOKEN))


# ── GitHub helpers ──────────────────────────────────────────────────────────

def _read_lines() -> tuple[list[str], str]:
    repo = gh.get_repo(GITHUB_REPO)
    contents = repo.get_contents(GITHUB_FILE_PATH)
    sha = contents.sha
    lines = [l for l in contents.decoded_content.decode("utf-8").splitlines() if l.strip()]
    return lines, sha

def _write_lines(lines: list[str], sha: str, msg: str) -> None:
    repo = gh.get_repo(GITHUB_REPO)
    repo.update_file(GITHUB_FILE_PATH, msg, "\n".join(lines) + "\n", sha)

def _find_user(lines: list[str], discord_id: str) -> tuple[int, list[str]] | tuple[None, None]:
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields and fields[0] == discord_id:
            return i, fields
    return None, None

async def run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args))


# ── Limpeza automática de licenças expiradas ─────────────────────────────────

def _is_license_expired(fields: list[str]) -> bool:
    """
    Replica exatamente a lógica do C#:
    - Se LicenseStartedAt for null → licença ainda não iniciada, não remove.
    - Se LicenseDurationDays <= 0 → licença permanente, não remove.
    - Caso contrário: expirada se UtcNow >= started + duration_days.
    """
    try:
        # fields: [discord_id, sid, is_active, products, duration_days, started, created_at]
        if len(fields) < 6:
            return False

        started_str = fields[5].strip()
        if not started_str or started_str == "null":
            return False  # Nunca iniciada, não expira

        duration_str = fields[4].strip()
        if not duration_str.lstrip("-").isdigit():
            return False
        duration_days = int(duration_str)
        if duration_days <= 0:
            return False  # Permanente

        started = datetime.fromisoformat(started_str)
        expires_at = started + timedelta(days=duration_days)
        return datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc)

    except Exception as e:
        log.warning(f"[CLEANUP] Erro ao verificar expiração: {e}")
        return False


def _do_cleanup() -> tuple[int, list[str]]:
    """Lê o arquivo, remove expirados e salva. Retorna (qtd_removida, ids_removidos)."""
    lines, sha = _read_lines()

    header_lines = []
    data_lines   = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FORMAT:") or stripped.startswith("PRODUCTS:"):
            header_lines.append(line)
        else:
            data_lines.append(line)

    removed_ids: list[str] = []
    kept_lines:  list[str] = []

    for line in data_lines:
        fields = line.split(":")
        if _is_license_expired(fields):
            removed_ids.append(fields[0])
            log.info(f"[CLEANUP] Licença expirada removida: {fields[0]}")
        else:
            kept_lines.append(line)

    if removed_ids:
        final_lines = header_lines + kept_lines
        _write_lines(
            final_lines,
            sha,
            f"[bot] auto-cleanup: removed {len(removed_ids)} expired license(s)"
        )

    return len(removed_ids), removed_ids


async def cleanup_loop():
    """Task que roda em background e limpa licenças expiradas periodicamente."""
    await bot.wait_until_ready()
    log.info(f"[CLEANUP] Task iniciada — intervalo: {CLEANUP_INTERVAL_MINUTES}min")
    while not bot.is_closed():
        try:
            log.info("[CLEANUP] Verificando licenças expiradas...")
            count, ids = await run_sync(_do_cleanup)
            if count:
                log.info(f"[CLEANUP] {count} licença(s) removida(s): {ids}")
            else:
                log.info("[CLEANUP] Nenhuma licença expirada encontrada.")
        except GithubException as e:
            log.error(f"[CLEANUP] Erro GitHub: {e}")
        except Exception as e:
            log.error(f"[CLEANUP] Erro inesperado: {e}")

        await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)


# ── Discord bot ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def setup_hook():
    log.info("Syncing slash commands...")
    await bot.tree.sync()
    log.info("Slash commands synced.")
    # Inicia a task de limpeza automática
    bot.loop.create_task(cleanup_loop())
    log.info("[CLEANUP] Task agendada.")

@bot.event
async def on_ready():
    log.info(f"Bot online as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="create", description="Cria uma nova licença para um usuário.")
@app_commands.describe(
    discord_id="ID do Discord do usuário",
    duration="Duração em dias",
    products="Produtos permitidos (separados por vírgula)",
)
async def cmd_create(interaction: discord.Interaction, discord_id: str, duration: int, products: str):
    log.info(f"[CMD] /create discord_id={discord_id} duration={duration} products={products}")
    await interaction.response.defer(ephemeral=True)

    def _do():
        lines, sha = _read_lines()
        idx, _ = _find_user(lines, discord_id)
        if idx is not None:
            return None, "Usuário já possui uma licença."
        new_line = f"{discord_id}:null:true:{products}:{duration}:null:{now_br()}"
        lines.append(new_line)
        _write_lines(lines, sha, f"[bot] create license for {discord_id}")
        return new_line, None

    try:
        result, err = await run_sync(_do)
        if err:
            await interaction.followup.send(f"Erro: {err}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"Licença criada para `{discord_id}`.\n```{result}```", ephemeral=True)
    except GithubException as e:
        log.error(f"GitHub error: {e}")
        await interaction.followup.send(f"Erro GitHub: {e}", ephemeral=True)


@bot.tree.command(name="remove", description="Remove a licença de um usuário.")
@app_commands.describe(discord_id="ID do Discord do usuário")
async def cmd_remove(interaction: discord.Interaction, discord_id: str):
    log.info(f"[CMD] /remove discord_id={discord_id}")
    await interaction.response.defer(ephemeral=True)

    def _do():
        lines, sha = _read_lines()
        idx, _ = _find_user(lines, discord_id)
        if idx is None:
            return False, "Usuário não encontrado."
        lines.pop(idx)
        _write_lines(lines, sha, f"[bot] remove license for {discord_id}")
        return True, None

    try:
        ok, err = await run_sync(_do)
        if err:
            await interaction.followup.send(f"Erro: {err}", ephemeral=True)
        else:
            await interaction.followup.send(f"Licença de `{discord_id}` removida.", ephemeral=True)
    except GithubException as e:
        log.error(f"GitHub error: {e}")
        await interaction.followup.send(f"Erro GitHub: {e}", ephemeral=True)


@bot.tree.command(name="reset_hwid", description="Reseta o HWID (SID) de um usuário.")
@app_commands.describe(discord_id="ID do Discord do usuário")
async def cmd_reset_hwid(interaction: discord.Interaction, discord_id: str):
    log.info(f"[CMD] /reset_hwid discord_id={discord_id}")
    await interaction.response.defer(ephemeral=True)

    def _do():
        lines, sha = _read_lines()
        idx, fields = _find_user(lines, discord_id)
        if idx is None:
            return False, "Usuário não encontrado."
        fields[1] = "null"
        lines[idx] = ":".join(fields)
        _write_lines(lines, sha, f"[bot] reset HWID for {discord_id}")
        return True, None

    try:
        ok, err = await run_sync(_do)
        if err:
            await interaction.followup.send(f"Erro: {err}", ephemeral=True)
        else:
            await interaction.followup.send(f"HWID de `{discord_id}` resetado.", ephemeral=True)
    except GithubException as e:
        log.error(f"GitHub error: {e}")
        await interaction.followup.send(f"Erro GitHub: {e}", ephemeral=True)


# ── Web API (aiohttp) ────────────────────────────────────────────────────────

async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def handle_bind(request: web.Request) -> web.Response:
    log.info(f"[API] POST /api/bind from {request.remote}")
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    discord_id = data.get("discord_id")
    sid = data.get("sid")
    if not discord_id or not sid:
        return web.json_response({"error": "discord_id and sid are required"}, status=400)

    def _do():
        lines, sha = _read_lines()
        idx, fields = _find_user(lines, discord_id)
        if idx is None:
            return False, "User not found"
        changed = False
        if fields[1] == "null":
            fields[1] = sid
            changed = True
            log.info(f"[API] Binding SID for {discord_id}")
        if fields[5] == "null":
            fields[5] = now_br()
            changed = True
            log.info(f"[API] Setting LICENSE_STARTED for {discord_id}")
        if changed:
            lines[idx] = ":".join(fields)
            _write_lines(lines, sha, f"[bot] bind SID for {discord_id}")
            log.info(f"[API] GitHub updated for {discord_id}")
        return True, None

    try:
        ok, err = await run_sync(_do)
        if err:
            return web.json_response({"error": err}, status=404)
        return web.json_response({"success": True})
    except GithubException as e:
        log.error(f"[API] GitHub error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def run_web_server():
    app = web.Application()
    app.router.add_get("/api/healthz", handle_healthz)
    app.router.add_post("/api/bind", handle_bind)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"[API] Web server running on port {PORT}")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    await run_web_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
