import * as AvatarPrimitive from "@radix-ui/react-avatar";
import type { ComponentPropsWithRef } from "react";
import { cn } from "@/lib/utils";

function Avatar({ className, ...props }: ComponentPropsWithRef<typeof AvatarPrimitive.Root>) {
	return (
		<AvatarPrimitive.Root
			className={cn("relative flex h-10 w-10 shrink-0 overflow-hidden rounded-full", className)}
			{...props}
		/>
	);
}

function AvatarFallback({
	className,
	...props
}: ComponentPropsWithRef<typeof AvatarPrimitive.Fallback>) {
	return (
		<AvatarPrimitive.Fallback
			className={cn(
				"flex h-full w-full items-center justify-center rounded-full bg-muted",
				className,
			)}
			{...props}
		/>
	);
}

export { Avatar, AvatarFallback };
