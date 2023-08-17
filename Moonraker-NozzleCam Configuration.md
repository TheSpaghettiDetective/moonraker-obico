# Moonraker-NozzleCam Configuration
## _First Layer AI Failure Detection_

[![N|Solid](https://www.obico.io/assets/images/obico-for-klipper-a2b728d12b37f82b73945f6d0e7131f6.png)](https://obico.io)

# Requirements

- Nozzle camera set up & configured in Mainsail / Fluidd 
- Configure you nozzle camera snapshot url in Obico (You should have received this in the sign up email)
- Slicer configured to notify Klipper / Moonraker of layer change

Follow along for a more detailed set up walkthrough

### Nozzle Camera
Pretty straightforward. 
- Buy & attach a nozzle camera to your 3D printer. Connect the USB to your Pi / SBC.
- Once it's all connected, add a new camera in your Fluidd / Mainsail settings & select the new one you just set up.
- Use mjpeg streaming & make sure the snapshot URL is working.(you will need this URL for the next step)

### Installation
If you have received the Alpha testing sign up link, open the configuration URL.
- Select the printer you'd like to configure from the dropdown. 
- Take the snapshot URL from the previous step & insert it into the text input.
- Click save.

## Slicer Configuration

Not everything in life wil be as easy as the first two steps.. We need to excplicitly tell moonraker when layer changes are occuring. I will show you how to acheive this in a couple types of slicers. The steps are pretty straightforward but some slicers can make this difficult (Cura).

## PrusaSlicer
Here is a helpful link from Mainsail on how to accomplish this in PrusaSlicer:
https://docs.mainsail.xyz/overview/slicer/prusaslicer

All you need to do is insert these lines:

Start G-code (before your start G-code):

```sh
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]
```

End G-code (at the last line):

```sh
; total layers count = [total_layer_count]
```

After layer change G-code:

```sh
SET_PRINT_STATS_INFO CURRENT_LAYER={layer_num + 1}
```

## SuperSlicer

Link from Mainsail on SuperSlicer:
https://docs.mainsail.xyz/overview/slicer/superslicer


Start G-code (before your start G-code):

```sh
SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]
```

After layer change G-code:

```sh
SET_PRINT_STATS_INFO CURRENT_LAYER={layer_num + 1}
```



The slicer configuration can be a headache. Please reach out if needed.
