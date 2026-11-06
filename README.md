# concurrency-control

Controller that serializes Tekton PipelineRuns on shared test machines. Each
PipelineRun is created `Pending` and names the machine(s) it needs through
labels; the controller starts it once those machines are `free`, marks them
`busy`, writes a warning to the machine's MOTD, and frees them when the run
(or the labelled task) finishes.

A web dashboard showing machine status, running pipelines and the log is served
on port 8080.

## Configuration

| Env var              | Meaning                                                              |
|----------------------|----------------------------------------------------------------------|
| `NAMESPACE`          | Namespace where PipelineRuns live                                    |
| `MACHINE_CM`         | ConfigMap holding `<machine>: free\|busy`                            |
| `PIPELINE_CM`        | ConfigMap holding the `wait_finish_runs` / `wait_finish_task` lists  |
| `LABEL_MACHINE_PR`   | PipelineRun label naming the machine held for the whole run          |
| `LAEBL_MACHINE_TASK` | PipelineRun label naming a machine held only during one task         |
| `LABEL_TASK`         | Name of the task that releases the `LAEBL_MACHINE_TASK` machine      |
| `API_TOKEN`          | Bearer token for the machine API; the write endpoints are off if unset |
| `TEMPLATE_REPO`      | GitHub repo with the Task templates for the pipeline builder, default `https://github.com/crc-org/ci-definitions` |
| `TEMPLATE_DIR`       | Read the templates from this local folder instead of GitHub (demo, offline work) |
| `PRESET_CM`          | ConfigMap holding the pipeline presets; presets are off if unset. Created on the first save |

## Machines

A machine has three parts:

1. **A key in `MACHINE_CM`.** The key is the machine name and the value is its
   status. The scheduler only treats `free` and `busy` as meaningful. Any other
   value, such as `maintenance`, keeps runs pending and logs a warning.
2. **A secret named after the machine**, with the keys `host`, `username` and
   `id_rsa`. The controller uses it over SSH to set and clear the MOTD. It
   reads the secret from `/opt/<machine>/` when it's mounted there. Otherwise
   it fetches the secret from the cluster at runtime, which needs `get` on
   secrets for the controller's service account.
3. **PipelineRuns that use it**, through the `LABEL_MACHINE_PR` or
   `LAEBL_MACHINE_TASK` label.

The controller picks the MOTD variant from the machine name: any name containing
`windows` is treated as a Windows host.

### Requirements on the machine

- An SSH server that accepts the public half of `id_rsa` for `username`.
- Linux: `username` can run `sudo tee /etc/motd` without a password.
- Windows: OpenSSH server with Git Bash as the default shell, because the
  controller runs `echo ... > C:\Users\<user>\ssh_motd.txt` through it.
- The tools the pipelines build with:
  - `git`
  - `make`
  - `golang`
  - `dotnet` (the Windows installer build)
  - Git Bash (Windows)

### Checking a machine

**Check** next to a machine in the web UI, after unlocking, logs in to it over
SSH and lists what it has and what's missing: whether the secret and key can be
read, whether login works, whether the host matches its name (Linux or Git
Bash), and the tools above with their versions. It runs only when you click
it. The commands only read, so it's safe on a busy machine too.

The commands run in a non-interactive SSH session, the same kind the pipelines
use. A tool that's only on the `PATH` of a login shell shows up as missing.
To change the list, edit `COMMON`, `LINUX` and `WINDOWS` in `checks.py`.

Any key type that `ssh-keygen` makes works: RSA, ECDSA or Ed25519.

### Adding a machine manually

```sh
oc create secret generic <machine> \
  --from-literal=host=<host> \
  --from-literal=username=<user> \
  --from-file=id_rsa=<path-to-private-key>
oc set data configmap $MACHINE_CM <machine>=free
```

That's enough because the controller reads unmounted secrets from the cluster.
To keep using mounts, add the secret as a volume at `/opt/<machine>` in the
Deployment and roll it out.

Then point the pipelines at the new machine through the
`LABEL_MACHINE_PR` / `LAEBL_MACHINE_TASK` label value.

### Removing a machine manually

1. **Drain it** so no new run gets scheduled on it:
   `oc set data configmap $MACHINE_CM <machine>=maintenance`
2. **Wait for running work to finish.** The machine must not appear in
   `wait_finish_runs` or `wait_finish_task` in `PIPELINE_CM`, or on the
   dashboard. Don't skip this: when a run finishes, the controller writes
   `<machine>=free` again and the machine comes back.
3. **Remove the machine:**
   ```sh
   oc set data configmap $MACHINE_CM <machine>-
   oc delete secret <machine>   # and drop the volume mount, if there is one
   ```
4. **Update the pipelines** that reference the machine. While they still do,
   their runs stay `Pending` forever and the controller logs a warning.

### Web UI

The dashboard on port 8080 can also add and remove machines. Click **Unlock**
and enter `API_TOKEN`. The token is kept for the browser tab only. You can then
fill in the **Add machine** form, or use **Remove** next to a machine. Busy
machines can't be removed. Without `API_TOKEN` on the controller the page is
read-only.

If the controller has no Route, reach the page with
`oc port-forward <controller-pod> 8080` and open <http://localhost:8080>.

### Machine API

`GET` is open. `POST` and `DELETE` need `Authorization: Bearer $API_TOKEN`.

```sh
# list machines and their status
curl http://<controller>:8080/api/machines

# add a machine (creates the secret and sets it free)
jq -n --arg key "$(cat id_rsa)" \
  '{name: "<machine>", host: "<host>", username: "<user>", private_key: $key}' |
curl -X POST -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d @- http://<controller>:8080/api/machines

# check what a machine has and what's missing (logs in over SSH)
curl -X POST -H "Authorization: Bearer $API_TOKEN" http://<controller>:8080/api/machines/<machine>/check

# remove a machine (409 while it is busy)
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" http://<controller>:8080/api/machines/<machine>
```

Pipelines, templates and presets work the same way:

```sh
curl http://<controller>:8080/api/pipelines   # Pipelines with their params and workspaces
curl http://<controller>:8080/api/templates   # the Task templates the builder offers

# create or replace a pipeline from templates; a step without "inputs" uses the template defaults
curl -X PUT -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"steps": [{"name": "build", "template": "crc-builder/tkn/crc-builder.yaml"}]}' \
  http://<controller>:8080/api/pipelines/crc-build-only
curl -X DELETE -H "Authorization: Bearer $API_TOKEN" http://<controller>:8080/api/pipelines/crc-build-only

curl http://<controller>:8080/api/presets

# create or replace a preset
curl -X PUT -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"pipeline": "crc-e2e", "machine": "linux-1", "params": {"crc-version": "2.45.0"},
       "workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}}}' \
  http://<controller>:8080/api/presets/e2e-linux

# start it: answers {"pipelinerun": "e2e-linux-x7k2p", ...}
curl -X POST -H "Authorization: Bearer $API_TOKEN" http://<controller>:8080/api/presets/e2e-linux/start

curl -X DELETE -H "Authorization: Bearer $API_TOKEN" http://<controller>:8080/api/presets/e2e-linux
```

Machine names must be DNS-1123 labels: lowercase letters, digits and `-`.
`DELETE` only removes a secret that the API created. Mounted secrets are left
alone because the Deployment owns them. The service account needs `create`,
`get`, `patch` and `delete` on secrets for these endpoints.

## Building pipelines

The **Pipelines** card lists the Tekton Pipelines in the namespace. After
unlocking, **New pipeline** builds one from the Task templates in
ci-definitions (`TEMPLATE_REPO`), always at `main`. Every YAML file in that repo
that holds a `kind: Task` is offered, except the `tpl/` sources and `samples/`.
The templates are only read. The Pipeline points at them with Tekton's git
resolver, so they can't be changed from here, and a later change on `main` is
picked up by the next run.

For each step you pick a template and say:

- **what it waits for.** A step waits for the previous one by default.
  Untick it to run steps in parallel. A step can also run **at the end,
  always**, as a Tekton `finally` task, even when other steps failed.
- **where each input comes from:**
  - **Template default:** the template's own default.
  - **Fixed value:** the same on every run.
  - **Ask when starting:** it becomes a Pipeline parameter that the preset
    fills in. Steps asking for the same input share one parameter.
  - **Output of an earlier step:** a result of an earlier step, for example
    `check-bundle-url → bundle-base-url`.

Template workspaces become Pipeline workspaces of the same name, and the
preset binds them. Pipelines are saved as `tekton.dev/v1` with the label
`concurrency-control/built=true`. Only those can be edited or deleted here. A
Pipeline made elsewhere is read-only, and its name can't be reused.

The controller reads the templates from GitHub, so it needs to reach
`api.github.com` and `raw.githubusercontent.com`. It keeps them for 10
minutes. The service account needs `get`, `list`, `create`, `patch` and
`delete` on `pipelines`.

## Pipeline presets

A preset saves everything needed to run a pipeline on a machine: the Pipeline,
its parameter values, how its workspaces are bound, the machine(s), and
optionally a service account and timeout. **Start** then creates a new
PipelineRun from it. The run is named `<preset>-<5 random characters>`, created
`Pending` with the machine labels, and labelled
`concurrency-control/preset=<preset>`. The controller starts it once the
machines are free, the same as any other run.

In the web UI, unlock and click **New preset**. Pick a Pipeline, and its
parameters and workspaces appear as fields. A parameter left empty uses the
Pipeline's default. Only string parameters can be set, array and object ones
always use their default. A workspace can be a new volume for each run, an
existing PVC, a secret, a ConfigMap or an empty directory.

A preset is checked against the Pipeline and the machines when it's saved, and
again on every start. If the Pipeline has gained a required parameter, or a
machine was removed, **Start** says so and the preset needs editing first.

A run that's waiting for its machine doesn't show under Running pipelines yet.
It appears there once the controller starts it.

Presets are stored as JSON in `PRESET_CM`, one key per preset. Runs are created
as `tekton.dev/v1`, which needs OpenShift Pipelines 1.11 or newer. The service
account needs `get` and `list` on `pipelines`, `create` on `pipelineruns`, and
`create`, `get` and `patch` on configmaps.

## Trying it locally

```sh
pip install -r requirements-dev.txt
python tests/demo_server.py          # or: python tests/demo_server.py 9000
```

Open <http://localhost:8080> and unlock with the token `demo`. The dashboard
runs against the same fake cluster and fake SSH as the tests, with a few demo
machines, two Pipelines and a preset. The pipeline builder offers the
templates from a `ci-definitions` clone next to this repo if there is one. It never touches a real cluster or
machine. The controller loop runs every 5 s, so a started preset gets scheduled
and its machine turns busy. Runs finish after a minute. **Check** on the demo
machines shows a missing `go`, a missing `dotnet` and an unreachable host. A
machine you add yourself checks as a healthy Linux host. Everything is kept in
memory and starts over on the next launch.

## Tests

The tests run against an in-memory fake of `oc` and never touch a real
cluster or machine.

```sh
pip install -r requirements-dev.txt
python -m pytest                     # API and browser tests, in the installed Google Chrome
python -m pytest tests/test_api.py   # API tests only, no browser needed
```

Without Google Chrome, run `python -m playwright install chromium` once and pass
`--browser chromium`. `--browser` also takes `firefox` and `webkit` and can be
repeated.

Tests marked `xfail` describe known bugs. Once a fix makes one pass, pytest
reports it as a failure so that the marker gets removed.
