// SPDX-License-Identifier: AGPL-3.0-only
// Receive standard kAEQuitApplication and Cmd-Q without letting AppKit exit.
// Python must finish DemoApp.close(), including owned-child cleanup proofs.
#import <AppKit/AppKit.h>
#import <Carbon/Carbon.h>

static BOOL quitRequested = NO;
static id previousDelegate;
static NSMenu *previousMenu;

@interface MyceliumQuitDelegate : NSObject <NSApplicationDelegate>
@end
@implementation MyceliumQuitDelegate
- (void)requestQuit:(id)sender {
    quitRequested = YES;
}
- (void)handleQuit:(NSAppleEventDescriptor *)event withReplyEvent:(NSAppleEventDescriptor *)reply {
    [self requestQuit:nil];
}
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    // Defensive fallback for direct terminate: callers, not the AppleEvent or
    // menu path. NSTerminateLater nests a modal loop and would block Python.
    [self requestQuit:sender];
    return NSTerminateCancel;
}
@end
static MyceliumQuitDelegate *quitDelegate;

int mycelium_quit_install(int headless) {
    @autoreleasepool {
        if (![NSThread isMainThread] || quitDelegate != nil) return 0;
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:headless ? NSApplicationActivationPolicyProhibited
                                         : NSApplicationActivationPolicyRegular];
        previousDelegate = [app delegate];
        previousMenu = [app mainMenu];
        quitRequested = NO;
        quitDelegate = [MyceliumQuitDelegate new];
        [app setDelegate:quitDelegate];
        NSMenu *menu = [NSMenu new];
        NSMenuItem *applicationItem = [NSMenuItem new];
        [menu addItem:applicationItem];
        NSMenu *applicationMenu = [NSMenu new];
        NSMenuItem *quit = [[NSMenuItem alloc] initWithTitle:@"Quit Mycelium A13"
            action:@selector(requestQuit:) keyEquivalent:@"q"];
        [quit setTarget:quitDelegate];
        [applicationMenu addItem:quit];
        [applicationItem setSubmenu:applicationMenu];
        [app setMainMenu:menu];
        [app finishLaunching];
        [[NSAppleEventManager sharedAppleEventManager] setEventHandler:quitDelegate
            andSelector:@selector(handleQuit:withReplyEvent:)
            forEventClass:kCoreEventClass andEventID:kAEQuitApplication];
        return 1;
    }
}

int mycelium_quit_poll(void) {
    @autoreleasepool {
        // Bounded, nonblocking pump: neither an event flood nor an idle app may
        // starve StopRequest/SIGTERM or the shared Python cleanup path.
        for (unsigned i = 0; i < 32 && !quitRequested; ++i) {
            NSEvent *event = [NSApp nextEventMatchingMask:NSEventMaskAny
                untilDate:[NSDate distantPast] inMode:NSDefaultRunLoopMode dequeue:YES];
            if (event == nil) break;
            [NSApp sendEvent:event];
        }
        return quitRequested ? 1 : 0;
    }
}

void mycelium_quit_uninstall(void) {
    @autoreleasepool {
        // The launcher exits normally only after its cleanup and finally blocks.
        [[NSAppleEventManager sharedAppleEventManager] removeEventHandlerForEventClass:kCoreEventClass
            andEventID:kAEQuitApplication];
        [NSApp setDelegate:previousDelegate];
        [NSApp setMainMenu:previousMenu];
        quitDelegate = nil;
        previousDelegate = nil;
        previousMenu = nil;
    }
}
