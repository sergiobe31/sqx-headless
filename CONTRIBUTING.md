# Cómo contribuir

> **English:** issues and pull requests in English are welcome. The docs are in Spanish; the
> process below is the same in any language.

Gracias por querer mejorar `sqx-headless`. Antes de nada, dos cosas que conviene saber.

## Cómo se mantiene este repo

Es el **espejo público** de un repo de trabajo privado. Cada actualización llega aquí como una
instantánea (commits `Export snapshot …` con la línea `Exported-From`). Con los PRs, el flujo es:

1. Abres el PR aquí y se revisa aquí.
2. Si se acepta, se fusiona aquí: tu commit queda en el historial con tu autoría.
3. El cambio se integra también en el repo original, para que la siguiente instantánea lo incluya
   en vez de pisarlo. El script de exportación se niega a sincronizar si hay cambios fusionados que
   no se han integrado.

Por eso, a veces una instantánea posterior reescribe tu cambio con otra forma (mismo efecto,
estilo del resto del código). No es un rechazo.

## Issues

Para un fallo, cuenta:

- versión de macOS y chip (Apple Silicon o Intel) y **build de SQX** (está probado con la 144);
- el comando o el botón del monitor que usaste y qué esperabas;
- los logs: `/tmp/<proyecto>_run/monitor.log`, el log de lanzamiento (`sqx/output/<proyecto>_launch.log`
  o `/tmp/<proyecto>_pertask_<hora>.log`), `sqx/output/<proyecto>_state.json` y lo que haya en
  `sqx/output/<proyecto>_<fecha>/`.

**No subas estrategias** (`.sqx`), databanks ni proyectos: son tu trabajo y no hacen falta para
diagnosticar. Si un log lleva rutas o nombres que prefieres no enseñar, recórtalos.

## Pull requests

- Una rama por cambio, PRs pequeños y con un objetivo claro.
- `python -m pytest -q` en verde, y la CI del PR también. Los tests **no necesitan SQX**: usan un
  motor simulado (ver `tests/test_pipeline_stop.py`). Un test nunca debe arrancar el `sqcli` real ni
  matar procesos que no haya creado él.
- Si el cambio toca la ejecución real, cuenta en el PR cómo lo probaste con SQX (build, tipo de
  proyecto, qué pasos).
- Sigue el estilo del código que tocas. Los comentarios en castellano o en inglés valen.
- Nada de rutas personales, claves, estrategias ni databanks en el PR.

Al enviar un PR aceptas que tu contribución se publique bajo la licencia MIT del repo.

## Se busca: versión para Windows

Ahora mismo solo funciona en macOS. Un port a Windows sería muy bienvenido, pero es un trabajo
grande; abre primero un issue para acordar el enfoque. Lo que implica, según lo visto hasta ahora:

- **Drivers en bash** (`run_pipeline_headless.sh`, `run_pipeline_per_task.sh`,
  `run_cascade_projects.sh`): reescribirlos en Python para que Mac y Windows compartan el código.
- **Detección y parada de procesos**: hoy usa `pgrep`/`pkill`/`ps` (unas 25 llamadas) y grupos de
  procesos POSIX (`start_new_session`, `killpg`). En Windows: `psutil` y grupos de procesos de
  Windows (`CREATE_NEW_PROCESS_GROUP` o *job objects*). Ojo: `os.kill(pid, 0)` en Windows no
  comprueba nada, envía Ctrl+C.
- **Mantener el equipo despierto**: `caffeinate` → `SetThreadExecutionState` o similar.
- **Rutas de la instalación de SQX**: `sqx/paths.py` busca la app en las rutas de macOS.
- **Tests**: los motores falsos son enlaces simbólicos con el nombre `sqcli`; en Windows harán falta
  ejecutables con ese nombre. Y un job `windows-latest` en la CI.
- Probarlo con un SQX real en Windows, que el mantenedor no puede hacer.

## Trato

Sé respetuoso y concreto. Las discusiones van sobre el código y los hechos, no sobre las personas.
