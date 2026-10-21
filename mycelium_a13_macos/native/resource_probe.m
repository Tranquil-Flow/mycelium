// SPDX-License-Identifier: AGPL-3.0-only
// One-shot, read-only macOS policy observation helper.
#import <Foundation/Foundation.h>
#import <Network/Network.h>

static NSString *thermalObservation(void) {
    switch ([NSProcessInfo processInfo].thermalState) {
        case NSProcessInfoThermalStateNominal:
            return @"nominal";
        case NSProcessInfoThermalStateFair:
            return @"fair";
        case NSProcessInfoThermalStateSerious:
            return @"serious";
        case NSProcessInfoThermalStateCritical:
            return @"critical";
    }
    return @"unknown";
}

static NSString *powerObservation(void) {
    @try {
        NSTask *task = [[NSTask alloc] init];
        NSPipe *stdoutPipe = [NSPipe pipe];
        NSPipe *stderrPipe = [NSPipe pipe];
        task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/pmset"];
        task.arguments = @[@"-g", @"batt"];
        task.standardInput = [NSFileHandle fileHandleWithNullDevice];
        task.standardOutput = stdoutPipe;
        task.standardError = stderrPipe;
        NSError *launchError = nil;
        if (![task launchAndReturnError:&launchError]) {
            return @"unknown";
        }
        [task waitUntilExit];
        if (task.terminationStatus != 0) {
            return @"unknown";
        }
        NSData *data = [[stdoutPipe fileHandleForReading] readDataToEndOfFile];
        NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        if ([text containsString:@"Now drawing from 'AC Power'"]) {
            return @"external";
        }
        if ([text containsString:@"Now drawing from 'Battery Power'"]) {
            return @"battery";
        }
    } @catch (__unused NSException *exception) {
        return @"unknown";
    }
    return @"unknown";
}

static NSString *networkObservation(void) {
    __block NSString *result = @"unknown";
    dispatch_semaphore_t ready = dispatch_semaphore_create(0);
    dispatch_queue_t queue = dispatch_queue_create(
        "org.mycelium.a13.resource-probe", DISPATCH_QUEUE_SERIAL
    );
    nw_path_monitor_t monitor = nw_path_monitor_create();
    nw_path_monitor_set_queue(monitor, queue);
    nw_path_monitor_set_update_handler(monitor, ^(nw_path_t path) {
        nw_path_status_t status = nw_path_get_status(path);
        if (status == nw_path_status_satisfied) {
            result = (nw_path_is_expensive(path) || nw_path_is_constrained(path))
                ? @"metered" : @"unmetered";
        } else if (status == nw_path_status_unsatisfied ||
                   status == nw_path_status_satisfiable) {
            result = @"unavailable";
        } else {
            result = @"unknown";
        }
        dispatch_semaphore_signal(ready);
    });
    nw_path_monitor_start(monitor);
    (void)dispatch_semaphore_wait(
        ready, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(NSEC_PER_SEC))
    );
    nw_path_monitor_cancel(monitor);
    __block NSString *snapshot = @"unknown";
    dispatch_sync(queue, ^{ snapshot = result; });
    return snapshot;
}

int main(void) {
    @autoreleasepool {
        NSDictionary<NSString *, NSString *> *observation = @{
            @"power": powerObservation(),
            @"thermal": thermalObservation(),
            @"network": networkObservation(),
        };
        NSError *error = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:observation
                                                       options:0
                                                         error:&error];
        if (json == nil || error != nil) {
            return 2;
        }
        [[NSFileHandle fileHandleWithStandardOutput] writeData:json];
        [[NSFileHandle fileHandleWithStandardOutput]
            writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
    }
    return 0;
}
