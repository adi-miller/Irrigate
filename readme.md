# Irrigate

## Installation

`pip3 install -r requirements.txt`

Copy config_sample.yaml to config.yaml and edit to match your location and preferences.

### Start as a Service (optional)

To enable irrigate to start as a SYSTEMD service edit `irrigate.service` according to your configuration (set execution path for `python3` as well as the location of `irrigate.py`) and then copy `irrigate.service` to `/lib/systemd/system` and set it's permissions to 644:

```bash
cp irrigate.service /lib/systemd/system
sudo chmod 644 /lib/systemd/system/irrigate.service
```

Next install the service and set it to run at system start up by executing the following commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable irrigate.service
```

To manually start / stop and to see the current status of the service, use the following commands respectively:

```
sudo systemctl start irrigate.service
sudo systemctl stop irrigate.service
sudo systemctl status irrigate.service
```

## Run

`python3 irrigate.py <config.yaml>`

## Instructions

- Valve `suspend` works while working and doesn't cancel the job. Suspension can be observed by examining secondsDaily
- Valve `enable` is tested before

## Operation

### Valves Enabled

Controlling the enablement of valves can be done using the `enabled` property in the configuration file, or using the `enabled` MQTT command.

When a valve is disabled (`enabled = false`) it doesn't get queued by the scheduler. This means that it's schedule is completely skipped (potentially causing the following schedules to run earlier).

If during processing of a job, it is determined the valve is disabled**, then the processing of that job will be terminated once it starts. This means that the `enable` MQTT command cannot be used to temporarily disable the operation. To temporarily stop the job without removing it from the queue, use `suspend`. This also means that the MQTT `queue` command doesn't work for a disabled valve.

  ** either from the configuration or from the MQTT `enable` command on a disabled valve.

### The difference between Suspend and Disable

Suspend doesn't cancel scheduling but only prevents openning the valve. This means that the job will still be scheduled and handled but the valve will not open. Suspend also works during the scheduling period, meaning that if the valve is already open, suspend will close it without cancelling the remaining time left for the schedule.

Enable is evaluated before scheduling, so if the valve is disabled (`enable=false`) the job will not be scheduled at all (unlike with suspended). If Enabled is set to False during the job execution then the valve is closed and the job is terminated.

## MQTT Commands

### Valve suspend

Suspend is used to force close the valve (for example in case the sensor is not providing the needed disable functionality). Suspending a valve is done using the `suspend` MQTT. Suspend cannot be used to unsuspend a valve when the sensor is disabling the operation.

It is used to prevent the valve from opening when a new job starts or to close the valve while the job is running. It doesn't prevent the job from being scheduled, and a suspended job takes up a thread from the concurrency pool.

### MQTT queue

This is used to manually queue a valve. Valve suspend is still respected, so a valve will not open if it is suspended. It does override any sensors because sensors are defined on a schedule.

The duration is specified in the payload of the `queue` command.

### MQTT forceopen

This is intended for testing. This command directly opens the valve ignoring all other conditions (concurrency settings, Enable, Suspend and Sensors). In addition, telemetry will not report that the valve is open, and when the program terminates, the valve will not be closed automatically. 

### MQTT forceclose

This is intended for testing. This command directly closes the valve. The program will not be aware of this close, so if the valve was opened from the queue, then the program will assume it is still open and will not process the next job in the queue (if available). 
