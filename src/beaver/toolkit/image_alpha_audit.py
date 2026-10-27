#!/usr/bin/env python3
"""Read-only PNG/image alpha audit after asset slicing. Requires Pillow.

Reports size, hash, alpha bounds and opaque border pixels. Does not crop, remove
backgrounds, infer body anchors, or certify a button's visual correctness.
Exit 0: requested checks pass; 1: review needed; 2: invalid input/dependency/I/O.
"""
import argparse
from beaver.toolkit.support.host.paths import Path
import sys
from beaver.toolkit._artifact_common import digest, validate_output, write_report


def inspect_image(path, max_pixels=20000000, expect_size=None, max_colors=None):
    if max_colors is not None and not 1<=max_colors<=65536:raise ValueError("max-colors must be 1..65536")
    if expect_size is not None and (len(expect_size)!=2 or any(type(x) is not int or x<1 for x in expect_size)):raise ValueError("Expected positive width and height")
    from PIL import Image
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected regular image file: ' + str(path))
    checksum = digest(path)
    try:
        source_image = Image.open(path)
    except Image.DecompressionBombError as error:
        raise ValueError('Unsafe image dimensions: ' + str(path)) from error
    with source_image as image:
        width, height = image.size
        if width * height > max_pixels:
            raise ValueError('Image exceeds max-pixels: ' + str(path))
        mode, image_format = image.mode, image.format
        if getattr(image,'n_frames',1)!=1:raise ValueError('Static image required')
        rgba=image.convert('RGBA');alpha=rgba.getchannel('A')
        colors=rgba.getcolors(maxcolors=max_colors+1) if max_colors is not None else None
        color_count=len(colors) if colors is not None else None
        histogram = alpha.histogram()
        strips = [alpha.crop((0, 0, width, 1))]
        if height > 1: strips.append(alpha.crop((0, height - 1, width, height)))
        if height > 2:
            strips.append(alpha.crop((0, 1, 1, height - 1)))
            if width > 1: strips.append(alpha.crop((width - 1, 1, width, height - 1)))
        border_pixels = sum(item.width * item.height for item in strips)
        border_opaque = sum(item.histogram()[255] for item in strips)
        result = {'path': str(path.resolve()), 'sha256': checksum, 'format': image_format,
                  'mode': mode, 'width': width, 'height': height,
                  'visible_bbox': alpha.getbbox(), 'alpha_extrema': alpha.getextrema(),
                  'transparent_pixels': histogram[0], 'partial_alpha_pixels': sum(histogram[1:255]),
                  'opaque_border_pixels': border_opaque, 'border_pixels': border_pixels,
                  'expected_size':list(expect_size) if expect_size is not None else None,
                  'size_matches':list(expect_size)==[width,height] if expect_size is not None else None,
                  'max_colors':max_colors,'color_count':color_count,
                  'colors_within_limit':color_count is not None and color_count<=max_colors if max_colors is not None else None}
    if digest(path) != checksum: raise ValueError('Source changed during inspection: ' + str(path))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-transparency', action='store_true', help='For cutout/button assets only; not full backgrounds')
    parser.add_argument('--expect-size',nargs=2,type=int,metavar=('WIDTH','HEIGHT'))
    parser.add_argument('--max-colors',type=int,help='Optional exact RGBA palette limit for pixel/palette tasks only')
    parser.add_argument('--max-pixels', type=int, default=20000000)
    args = parser.parse_args()
    try:
        if args.max_pixels < 1: raise ValueError('max-pixels must be positive')
        validate_output(args.output, args.images)
        rows = [inspect_image(path, args.max_pixels,args.expect_size,args.max_colors) for path in args.images]
        issues = []
        for row in rows:
            if row['size_matches'] is False:issues.append({'path':row['path'],'kind':'unexpected_dimensions'})
            if row['colors_within_limit'] is False:issues.append({'path':row['path'],'kind':'too_many_rgba_colors'})
            if row['visible_bbox'] is None:
                issues.append({'path': row['path'], 'kind': 'fully_transparent_image'})
            elif args.require_transparency and row['transparent_pixels'] == 0:
                issues.append({'path': row['path'], 'kind': 'no_fully_transparent_pixels'})
        report = {'schema': 'image-alpha-audit-v1', 'images': rows, 'issues': issues,
                  'limits': ['Opaque edges are diagnostic, not automatically an error.',
                             'Does not validate crop content, premultiplied blend, pivots or actual pressed-state rendering.']}
        write_report(report, args.output, args.images)
        print('IMAGE_ALPHA_AUDIT images=' + str(len(rows)) + ' issues=' + str(len(issues)))
        return 1 if issues else 0
    except (OSError, ValueError, ImportError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__': sys.exit(main())
