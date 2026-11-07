# Project workflow

- After each code change, run the checks appropriate to that change, then apply the update to the running local application. Confirm the old Reader process exits before starting the new one; launching StartReader.vbs while a healthy old service exists does not restart it.
- If FurinaTTS code or model configuration changed, restart and verify that service as well. Unrelated reader-only changes do not require unloading the model.
- Refresh the reader page after the backend restart so the frontend and backend use the same revision. Preserve the reading position and player settings. Browser interaction still requires the user's explicit browser-operation authorization under their global instructions; if unavailable, complete service verification and clearly identify the remaining refresh.
- Check health, the served frontend, book metadata and relevant behavior after restart. Do not equate a successful launch command with an applied update. Reuse valid audio caches; invalidate only the affected audio when synthesis behavior changes.
