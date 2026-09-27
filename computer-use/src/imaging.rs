//! Thumbnail, resize and PNG -- the same algorithm as the Python reference (PRP-0191 1.1).
//!
//! * thumbnail: integer box-reduce of the RGB capture (Pillow ``Image.reduce``: sizes rounded
//!   up, an edge box averages only the pixels it covers), then Pillow's ``L`` luma
//!   (ITU-R 601-2, fixed point, rounded), then a bilinear convolution to 192x108;
//! * encode: an integer box-reduce while at least 2x of the scale remains (Pillow's
//!   ``reducing_gap=2.0``), then a Catmull-Rom (Pillow ``BICUBIC``) convolution, then the
//!   fastest PNG compression.

use fast_image_resize::images::Image;
use fast_image_resize::{FilterType, PixelType, ResizeAlg, ResizeOptions, Resizer};

use crate::protocol::{THUMB_H, THUMB_W};

/// A packed 8-bit RGB image.
#[derive(Clone)]
pub struct RgbImage {
    pub width: u32,
    pub height: u32,
    pub data: Vec<u8>,
}

impl RgbImage {
    pub fn new(width: u32, height: u32, data: Vec<u8>) -> Self {
        debug_assert_eq!(data.len(), (width * height * 3) as usize);
        Self { width, height, data }
    }

    /// From a top-down BGRA / BGRX buffer (GDI and DXGI give that layout).
    pub fn from_bgra(width: u32, height: u32, bgra: &[u8], stride: usize) -> Self {
        let mut data = Vec::with_capacity((width * height * 3) as usize);
        for y in 0..height as usize {
            let row = &bgra[y * stride..y * stride + width as usize * 4];
            for px in row.chunks_exact(4) {
                data.extend_from_slice(&[px[2], px[1], px[0]]);
            }
        }
        Self::new(width, height, data)
    }
}

/// Pillow ``Image.reduce((fx, fy))`` for ``channels`` interleaved 8-bit channels.
fn box_reduce(src: &[u8], w: u32, h: u32, channels: usize, fx: u32, fy: u32) -> (Vec<u8>, u32, u32) {
    let ow = w.div_ceil(fx);
    let oh = h.div_ceil(fy);
    let mut out = vec![0u8; (ow * oh) as usize * channels];
    let mut acc = vec![0u32; channels];
    for oy in 0..oh {
        let y0 = oy * fy;
        let y1 = (y0 + fy).min(h);
        for ox in 0..ow {
            let x0 = ox * fx;
            let x1 = (x0 + fx).min(w);
            acc.iter_mut().for_each(|a| *a = 0);
            for y in y0..y1 {
                let row = (y * w) as usize * channels;
                for x in x0..x1 {
                    let i = row + x as usize * channels;
                    for c in 0..channels {
                        acc[c] += src[i + c] as u32;
                    }
                }
            }
            let n = (y1 - y0) * (x1 - x0);
            let o = ((oy * ow + ox) as usize) * channels;
            for c in 0..channels {
                out[o + c] = ((acc[c] + n / 2) / n) as u8;
            }
        }
    }
    (out, ow, oh)
}

/// Pillow ``convert("L")``: ``(R*19595 + G*38470 + B*7471 + 0x8000) >> 16``.
fn luma(rgb: &[u8]) -> Vec<u8> {
    rgb.chunks_exact(3)
        .map(|p| ((p[0] as u32 * 19595 + p[1] as u32 * 38470 + p[2] as u32 * 7471 + 0x8000) >> 16) as u8)
        .collect()
}

fn resize(src: Vec<u8>, w: u32, h: u32, pixel: PixelType, tw: u32, th: u32, filter: FilterType) -> Vec<u8> {
    if (w, h) == (tw, th) {
        return src;
    }
    let src_img = Image::from_vec_u8(w, h, src, pixel).expect("source buffer matches its size");
    let mut dst = Image::new(tw, th, pixel);
    let options = ResizeOptions::new().resize_alg(ResizeAlg::Convolution(filter));
    Resizer::new()
        .resize(&src_img, &mut dst, &options)
        .expect("same pixel type on both sides");
    dst.into_vec()
}

/// The 192x108 8-bit grayscale thumbnail of a capture (the stability poll's input).
pub fn gray_thumbnail(img: &RgbImage) -> Vec<u8> {
    let factor = (img.width / THUMB_W).min(img.height / THUMB_H).max(1);
    let (rgb, w, h) = if factor > 1 {
        box_reduce(&img.data, img.width, img.height, 3, factor, factor)
    } else {
        (img.data.clone(), img.width, img.height)
    };
    let gray = luma(&rgb);
    resize(gray, w, h, PixelType::U8, THUMB_W, THUMB_H, FilterType::Bilinear)
}

/// The capture resized to ``w`` x ``h``, as PNG bytes.
pub fn encode_png(img: &RgbImage, w: u32, h: u32) -> Result<Vec<u8>, String> {
    let (mut data, mut cw, mut ch) = (img.data.clone(), img.width, img.height);
    if (cw, ch) != (w, h) {
        // Pillow reducing_gap=2.0: box-reduce by the integer part that keeps >= 2x of the scale.
        let fx = ((cw as f64 / w as f64) / 2.0).floor().max(1.0) as u32;
        let fy = ((ch as f64 / h as f64) / 2.0).floor().max(1.0) as u32;
        if fx > 1 || fy > 1 {
            (data, cw, ch) = box_reduce(&data, cw, ch, 3, fx, fy);
        }
        data = resize(data, cw, ch, PixelType::U8x3, w, h, FilterType::CatmullRom);
    }
    let mut out = Vec::with_capacity(data.len() / 3);
    {
        let mut encoder = png::Encoder::new(&mut out, w, h);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Fast);
        let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
        writer.write_image_data(&data).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The synthetic image the golden files were made from (PRP-0191 2.6).
    fn synth(w: u32, h: u32) -> RgbImage {
        let mut data = Vec::with_capacity((w * h * 3) as usize);
        for y in 0..h {
            for x in 0..w {
                data.push(((x * 7 + y * 3) % 256) as u8);
                data.push(((x * y) % 251) as u8);
                data.push(((x ^ y) % 256) as u8);
            }
        }
        RgbImage::new(w, h, data)
    }

    fn assert_close(w: u32, h: u32, golden: &[u8]) {
        let ours = gray_thumbnail(&synth(w, h));
        assert_eq!(ours.len(), golden.len());
        let diffs: Vec<i32> = ours.iter().zip(golden).map(|(a, b)| (*a as i32 - *b as i32).abs()).collect();
        let max = *diffs.iter().max().unwrap();
        let mean = diffs.iter().sum::<i32>() as f64 / diffs.len() as f64;
        // The engine compares 4x4 block MEANS against a delta of 6 (perception.py): a
        // per-pixel difference of a few levels from filter rounding changes nothing.
        assert!(mean <= 1.0 && max <= 8, "{w}x{h}: mean {mean:.3}, max {max}");
    }

    #[test]
    fn thumbnail_matches_the_python_reference() {
        assert_close(1000, 700, include_bytes!("../tests/golden/thumb_1000x700.bin"));
        assert_close(300, 200, include_bytes!("../tests/golden/thumb_300x200.bin"));
        assert_close(2880, 1824, include_bytes!("../tests/golden/thumb_2880x1824.bin"));
    }

    #[test]
    fn reduce_matches_pillow_edges() {
        // Pillow: a 4x1 image reduced by 3 -> 2x1, the edge box averages only its pixel.
        let src = [0, 0, 0, 0, 0, 0, 0, 0, 0, 90, 90, 90];
        let (out, w, h) = box_reduce(&src, 4, 1, 3, 3, 3);
        assert_eq!((w, h), (2, 1));
        assert_eq!(out, vec![0, 0, 0, 90, 90, 90]);
    }

    #[test]
    fn luma_matches_pillow() {
        assert_eq!(luma(&[10, 200, 30]), vec![124]);
    }

    #[test]
    fn encode_produces_a_png_of_the_asked_size() {
        let png = encode_png(&synth(640, 400), 320, 200).unwrap();
        assert_eq!(&png[1..4], b"PNG");
        let decoder = png::Decoder::new(std::io::Cursor::new(png));
        let reader = decoder.read_info().unwrap();
        assert_eq!((reader.info().width, reader.info().height), (320, 200));
    }
}
