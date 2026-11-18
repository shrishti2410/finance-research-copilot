// The same card as the Open Graph image (app/opengraph-image.tsx).
import Image from "./opengraph-image";
import { HEADLINE, SITE_NAME } from "@/lib/site";

export const alt = `${SITE_NAME}: ${HEADLINE}`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default Image;
