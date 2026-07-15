import type { ComponentPropsWithRef } from "react";
import { cn } from "@/lib/utils";

/**
 * Placeholder block shown while content loads (shadcn/ui `skeleton`).
 *
 * Prefer this over a spinner when the shape of the incoming content is known: a
 * skeleton tells the user *what* is arriving and reserves its space, so the list
 * does not jump when it lands.
 */
function Skeleton({ className, ...props }: ComponentPropsWithRef<"div">) {
	return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}

export { Skeleton };
