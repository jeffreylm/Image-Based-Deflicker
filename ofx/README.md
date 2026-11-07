# Resolve OFX frame-access probe

This initial contribution establishes source-frame access in a temporal OpenFX filter. It displays the previous, current and next frames in three columns. The alternative original view copies the current input exactly. It is a diagnostic foundation for a future deflicker integration. It currently performs no flicker correction.

The probe is newly written integration code. The existing Python processing code is unchanged. Adapting that algorithm is pending clarification of the upstream licence and maintainer direction.

## Build and check

On an Apple Silicon Mac with Xcode Command Line Tools, run `sh ofx/build.sh` and `sh ofx/test.sh` from the repository root. The build is CPU-only float RGB/RGBA and uses the native machine architecture. Other platforms have not been built or tested.

The bundle is `ofx/build/ImageBasedDeflickerProbe.ofx.bundle`. Copy it into `/Library/OFX/Plugins/` and restart Resolve. Add **Image-Based Deflicker Frame Probe** from the **Image-Based Deflicker** OFX category to an Edit-page clip or Fusion node graph. To remove it, quit Resolve and move that bundle out of the plug-in folder.

The default view shows earlier/current/later frames in the left/middle/right columns. Choose **Original** to check exact pass-through. The frame offset controls the distance from the current frame. Requests are clamped to the source clip range. Missing requested frames produce an explicit render failure instead of silently repeating the current frame. A numeric diagnostic log is optional and disabled by default. Choose its path explicitly. It records requested times, image times when provided, dimensions and pixel hashes. It does not record source filenames or image data. The log is capped at 2,000 records per loaded plug-in binary.

## Tests

The independent host tests exercise the exported OFX lifecycle and rendering actions. They cover frame declarations, nonzero clip starts, boundaries, out-of-order requests, exact bypass, signed row strides, RGB/RGBA buffers, missing frames, cancellation and image-handle release. Resolve acceptance is recorded separately after the plug-in is installed and the visible output has been inspected in both pages.

### Initial Resolve acceptance

The probe was installed and inspected in Resolve Studio 21.2.0.6 on Apple Silicon. It appears in both the Edit effects library and Fusion's node registry. The sampled image hashes for neighbours requested at time 150 match independent current-frame requests at 149 and 151, and repeat after seeking elsewhere. A 1920×1080 source also gives matching sampled buffers in both pages at tested source times, including frame zero clamped to `[0, 0, 1]`. Fusion Original mode produced a viewer image identical to node bypass. Saving and reopening the dedicated test project is part of the local acceptance record.

For a 4096×2160 source in a 1920×1080 unmanaged timeline, Fusion supplied 4096×2160 buffers while the Edit effect received 1920×1080 buffers. Future analysis caches and detail settings must account for this difference. The host did not provide the optional image-time property, so diagnostics log it as `null`; matching source hashes provide the frame-identity evidence.

## Licence and dependencies

The integration's licensing status is described in `ofx/NOTICE.md` and is awaiting maintainer clarification. Vendored OpenFX headers retain their BSD licence in `ofx/vendor/openfx/LICENSE.md`. They are from the Academy Software Foundation OpenFX repository at commit `1769a91c3a5dd0b859a0618395bb08165486460d`.
