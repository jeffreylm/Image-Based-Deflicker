# Processing integration decisions

This frame probe is the first milestone. It validates temporal requests before committing to a processing architecture. The Python algorithm has not been ported.

## Input and colour

Process the image buffers supplied by the host at the effect's position in the graph. Reopening the camera file would bypass upstream Fusion nodes, sizing and other source changes. The current Python path explicitly decodes bounded Rec.709 BGR pixels. A float OFX path therefore needs an explicit input-space contract, reversible conversion where supported, channel ordering and premultiplication tests. Host discovery alone does not establish the colour space of a buffer.

## Analysis scope

Automatic period estimation and the detail-coherence mask use shot-wide information in the Python implementation. A production integration needs an explicit shot range and source revision, cancellable analysis, bounded storage and a visible analysis status. A timeline trim can leave source handles accessible beyond the visible edit. Source availability must not be confused with permission to analyse across a shot boundary.

## Scheduling and ownership

Request source frames only through permitted OFX host actions, and release every image handle before the action ends. Any analysis worker receives copies owned by the plug-in. Rendering must work when the host requests frames out of order, and must not depend on the history of earlier playback. Cancellation and failed analysis leave the last complete cache distinguishable from a partial result.

## Subsequent milestones

1. Agree contribution terms and the intended integration structure with the maintainer.
2. Establish an in-memory reference against the Python brightness and colour stages, using a controlled periodic test and a representative original projector shot.
3. Implement and verify brightness and colour correction, motion handling and masking.
4. Add detail-pulse correction and optional temporal denoising, with independent enable controls.
5. Validate analysis invalidation, multiple instances, project reopening, colour conversion and HD/UHD processing costs.

Use numerical checks alongside moving-image inspection. Reduced periodic energy alone does not establish preserved motion or an acceptable visual result. Performance measurements should specify the host page, image dimensions, diagnostics state and whether source decoding is included.
