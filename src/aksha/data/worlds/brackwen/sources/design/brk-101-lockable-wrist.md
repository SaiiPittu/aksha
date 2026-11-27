---
doc: SYN-BW-DN-101
idea: BRK-101
written: '2026-02-19'
synthetic: true
---
> SYNTHETIC FIXTURE. Invented for testing the agents; not a real company, person, product or document.

# BRK-101: a wrist that is soft on contact and stiff in transit

Owner: Ravi Anand, mechanical lead, Brackwen Robotics
Written: 19 February 2026
Reviewed by: Lena Brandt (controls)

## Problem

The picking cell grabs mixed items out of totes. A stiff wrist knocks items over and dents boxes when the fingers first touch them; a soft wrist lets the item swing on the way to the outbound tote, and swung items get dropped on the conveyor edge.

## Sequence

E1. Approach the item with the wrist free: an elastomer flexure lets the gripper tilt and shift a few millimetres when the fingers touch, so the contact does not push the item over.
E2. Lock the wrist stiff with the electro-permanent-magnet clutch once the measured contact force has stayed above 4 N for 80 milliseconds without a break.
E3. While carrying, release the clutch on its own when the wrist torque passes 1.2 N·m, and stop the arm, so a snagged item or a person in the way meets a soft wrist again.
E4. After every release, re-zero the wrist force-torque sensor with the gripper empty before the next approach.

## Numbers

- Lock force threshold: 4 N
- Settle window before locking: 80 milliseconds
- Breakaway torque: 1.2 N·m
- Clutch switching time: 12 ms (measured, not a setting)

## Tried and dropped

- Locking the wrist a fixed 150 ms after the first contact. Dropped: soft polybags were still settling at 150 ms and boxes had settled long before, so the delay was wrong for both.
- A spring-loaded wrist that is never locked. Dropped: items swung up to 30 mm on fast moves.

## Bench evidence

3,000 picks on the mixed-SKU bench, March to May 2026. Knock-overs fell from 6.1% with the stiff wrist to 0.9%. Bench run log SYN-BW-RUN-0412.

## Open points

- The 4 N lock threshold came from bench tuning; nobody wrote down why 4 N and not something lower for light items.
- Who suggested the breakaway release: see the thread with the contractor. To be settled before anything is filed.
