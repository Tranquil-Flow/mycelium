// SPDX-License-Identifier: AGPL-3.0-only
// Plain non-Mycelium control app for the bundle smoke's focus judgement: one titled window,
// ordered front WITHOUT any explicit activation request, so whatever the host does to the
// frontmost app when a new regular app shows a window is what this control shows.
// argv[1] = "activate" additionally asks for activation (not used by the smoke).
#import <Cocoa/Cocoa.h>
int main(int argc, char **argv) {
  @autoreleasepool {
    BOOL activate = argc > 1 && strcmp(argv[1], "activate") == 0;
    NSApplication *app = [NSApplication sharedApplication];
    [app setActivationPolicy:NSApplicationActivationPolicyRegular];
    NSWindow *w = [[NSWindow alloc] initWithContentRect:NSMakeRect(200,200,400,300)
        styleMask:(NSWindowStyleMaskTitled|NSWindowStyleMaskClosable) backing:NSBackingStoreBuffered defer:NO];
    [w setTitle:@"L11 focus control"];
    [app finishLaunching];
    if (activate) { [app activateIgnoringOtherApps:YES]; [w makeKeyAndOrderFront:nil]; }
    else { [w orderFront:nil]; }
    printf("CONTROL_WINDOW_SHOWN\n"); fflush(stdout);
    [app run];
  }
  return 0;
}
