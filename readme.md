# Irrigate

## Installation

`pip install -r requirements.txt`

Copy config_sample.yaml to config.yaml and edit to match your location and preferences. 

## Run

`python3 irrigate.py <config.yaml>`

## Instructions

- Valve `suspend` works while working and doesn't cancel the job. Suspension can be observed by examining openSeconds
- Valve `enable` is tested before

### Valve Suspend

Suspend is used to replce the operation of the sensors in case they are not providing the needed disable functionality. Suspending a valve is done via MQTT. It is used to prevent the valve from opening when a new job starts or to close the valve while the job is running. It doesn't preven the job from being scheduled, and a suspended job takes up a thread from the concurrency pool.

### Valve Disable (enabled = False)

Is a master kill switch for MQTT as well.

### MQTT Open

This is used to manually open a valve. Valve suspend is still respected, so a valve will not open
if it is suspended. It does override any sensors because sensors are defined on a schedule.

### The difference between Suspend and Disable

Suspend doesn't cancel scheduling but only prevents openning the valve. This means that the job will still be scheduled and handled but the valve will not open. Suspend also works during the scheduling period, meaning that if the valve is already open, suspend will close it without cancelling the remaining time left for the schedule.

Enable is tested before scheduling, so if the valve is disabled (enable=false) the job will not be scheduled at all (unlike with suspended). If Enabled is set to False during the job execution then the valve is closed and the job is terminated.

The MQTT 'open' command ignores valve.enabled.
