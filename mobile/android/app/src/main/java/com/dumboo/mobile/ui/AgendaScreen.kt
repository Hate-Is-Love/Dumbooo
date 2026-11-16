package com.dumboo.mobile.ui

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import android.widget.Toast
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateIntAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.dumboo.mobile.Agenda
import com.dumboo.mobile.AgendaEvent
import com.dumboo.mobile.AgendaTask
import com.dumboo.mobile.Bridge
import kotlinx.coroutines.delay
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

private val Pink = Color(0xFFFF6B9A)
private val Mint = Color(0xFF62E6B0)

/** Reminders and schedule: animated, and you can add, tick off and swipe away tasks and events from the phone. */
@Composable
fun AgendaScreen() {
    val context = LocalContext.current
    val agenda by Bridge.agenda.collectAsStateWithLifecycle()
    var tab by remember { mutableIntStateOf(0) }
    var adding by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        Bridge.requestAgenda()
        Bridge.agendaError.collect { Toast.makeText(context, it, Toast.LENGTH_LONG).show() }
    }
    Box(Modifier.fillMaxSize().background(Night)) {
        Column(Modifier.fillMaxSize()) {
            Header(agenda)
            TabRow(selectedTabIndex = tab, containerColor = Night, contentColor = Lilac) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("SCHEDULE") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("TASKS") })
            }
            val a = agenda
            if (a == null) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Loading() }
            } else {
                AnimatedContent(tab, transitionSpec = {
                    (slideInHorizontally { w -> if (targetState > initialState) w else -w } + fadeIn()) togetherWith
                        (slideOutHorizontally { w -> if (targetState > initialState) -w else w } + fadeOut())
                }, label = "agendaTab") { t ->
                    if (t == 0) Schedule(a.events) else Tasks(a.tasks)
                }
            }
        }
        FloatingActionButton(onClick = { adding = true }, containerColor = Lilac, contentColor = Ink,
            modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp)) {
            Icon(Icons.Filled.Add, "Add")
        }
    }
    if (adding) AddDialog(startWithEvent = tab == 0) { adding = false }
}

@Composable
private fun Header(agenda: Agenda?) {
    val today = LocalDate.now()
    val todays = agenda?.events?.count { it.startsAt.startsWith(today.toString()) } ?: 0
    val open = agenda?.tasks?.size ?: 0
    val overdue = agenda?.tasks?.count { it.overdue } ?: 0
    val shownToday by animateIntAsState(todays, tween(700), label = "today")
    val shownOpen by animateIntAsState(open, tween(700), label = "open")
    Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(today.format(DateTimeFormatter.ofPattern("EEEE", Locale.getDefault())).uppercase(),
                color = Lilac, fontFamily = FontFamily.Monospace, fontSize = 13.sp, letterSpacing = 2.sp)
            Text(today.format(DateTimeFormatter.ofPattern("d MMMM", Locale.getDefault())), fontSize = 26.sp,
                fontWeight = FontWeight.Bold, color = Color.White)
            Text("$shownToday today · $shownOpen tasks" + if (overdue > 0) " · $overdue overdue" else "",
                color = if (overdue > 0) Pink else Muted, fontSize = 13.sp)
        }
        Ring(done = (open - overdue).coerceAtLeast(0), total = open)
        IconButton(onClick = { Bridge.requestAgenda() }) { Icon(Icons.Filled.Refresh, "Refresh", tint = Lilac) }
    }
}

/** A progress ring: how many open tasks are on time. */
@Composable
private fun Ring(done: Int, total: Int) {
    val sweep = remember { Animatable(0f) }
    LaunchedEffect(done, total) { sweep.animateTo(if (total == 0) 360f else 360f * done / total, tween(900, easing = FastOutSlowInEasing)) }
    Box(Modifier.size(54.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            drawArc(Color(0xFF2E2658), 0f, 360f, false, style = Stroke(10f))
            drawArc(Brushes.ring, -90f, sweep.value, false, style = Stroke(10f, cap = StrokeCap.Round),
                topLeft = Offset.Zero, size = Size(size.width, size.height))
        }
        Text(if (total == 0) "✓" else "$done/$total", color = Color.White, fontSize = 12.sp)
    }
}

private object Brushes {
    val ring = androidx.compose.ui.graphics.Brush.sweepGradient(listOf(Lilac, Cyan, Mint, Lilac))
}

@Composable
private fun Loading() {
    val t = rememberInfiniteTransition(label = "load")
    val a by t.animateFloat(0.3f, 1f, infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "a")
    Text("Loading from the laptop…", color = Lilac.copy(alpha = a))
}

/** Staggered slide-and-fade in for list items. */
@Composable
private fun Appear(index: Int, content: @Composable () -> Unit) {
    val anim = remember { Animatable(0f) }
    LaunchedEffect(Unit) { delay(index * 45L); anim.animateTo(1f, tween(380, easing = FastOutSlowInEasing)) }
    Box(Modifier.graphicsLayer { alpha = anim.value; translationX = (1 - anim.value) * 60f }) { content() }
}

private fun dayLabel(date: LocalDate): String {
    val today = LocalDate.now()
    return when (date) {
        today -> "Today"
        today.plusDays(1) -> "Tomorrow"
        else -> date.format(DateTimeFormatter.ofPattern("EEEE d MMM", Locale.getDefault()))
    }
}

@Composable
private fun Schedule(events: List<AgendaEvent>) {
    if (events.isEmpty()) { Empty("Nothing scheduled in the next 7 days.", "Tap + to add an event, or say “schedule a meeting tomorrow at 3”."); return }
    val now = LocalDateTime.now()
    val next = events.minByOrNull { it.startsAt }?.id
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp, 8.dp, 16.dp, 96.dp)) {
        var lastDay: LocalDate? = null
        events.sortedBy { it.startsAt }.forEachIndexed { i, e ->
            val start = runCatching { LocalDateTime.parse(e.startsAt) }.getOrNull()
            val day = start?.toLocalDate()
            if (day != null && day != lastDay) {
                lastDay = day
                item(key = "d$day") {
                    Text(dayLabel(day).uppercase(), color = Lilac, fontFamily = FontFamily.Monospace, fontSize = 12.sp,
                        letterSpacing = 2.sp, modifier = Modifier.padding(top = 14.dp, bottom = 6.dp))
                }
            }
            item(key = "e${e.id}") {
                Appear(i) {
                    Swipe(onDelete = { Bridge.deleteEvent(e.id) }) {
                        EventCard(e, start, isNext = e.id == next, now = now)
                    }
                }
            }
        }
    }
}

@Composable
private fun EventCard(e: AgendaEvent, start: LocalDateTime?, isNext: Boolean, now: LocalDateTime) {
    val glow = rememberInfiniteTransition(label = "next")
    val g by glow.animateFloat(0.35f, 1f, infiniteRepeatable(tween(1100), RepeatMode.Reverse), label = "g")
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.width(56.dp)) {
            Text(start?.format(DateTimeFormatter.ofPattern("HH:mm")) ?: "", color = Color.White,
                fontFamily = FontFamily.Monospace, fontSize = 15.sp)
        }
        Box(Modifier.size(12.dp).clip(CircleShape).background(if (isNext) Lilac.copy(alpha = g) else Color(0xFF3B3270)))
        Spacer(Modifier.width(10.dp))
        Card(colors = CardDefaults.cardColors(containerColor = if (isNext) Color(0xFF2E2563) else Color(0xFF221B42)),
            shape = RoundedCornerShape(14.dp), modifier = Modifier.weight(1f)) {
            Column(Modifier.padding(12.dp)) {
                Text((if (e.reminder) "⏰  " else "") + e.title, color = Color.White, fontWeight = FontWeight.Medium)
                if (isNext && start != null) {
                    val mins = java.time.Duration.between(now, start).toMinutes()
                    if (mins >= 0) Text(when {
                        mins < 60 -> "in $mins min"
                        mins < 24 * 60 -> "in ${mins / 60} h ${mins % 60} min"
                        else -> "in ${mins / (24 * 60)} days"
                    }, color = Lilac, fontSize = 12.sp)
                }
            }
        }
    }
}

@Composable
private fun Tasks(tasks: List<AgendaTask>) {
    if (tasks.isEmpty()) { Empty("No tasks. Nice.", "Tap + to add one, or say “add a task to buy milk”."); return }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp, 12.dp, 16.dp, 96.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        itemsIndexed(tasks.sortedWith(compareByDescending<AgendaTask> { it.overdue }.thenBy { it.due ?: "9999" }),
            key = { _, t -> t.id }) { i, t ->
            Appear(i) { Swipe(onDelete = { Bridge.deleteTask(t.id) }) { TaskCard(t) } }
        }
    }
}

@Composable
private fun TaskCard(t: AgendaTask) {
    var done by remember(t.id) { mutableStateOf(false) }
    val strike by animateColorAsState(if (done) Muted else Color.White, label = "strike")
    LaunchedEffect(done) { if (done) { delay(650); Bridge.completeTask(t.id) } }
    val bar = when (t.priority) { "high" -> Pink; "low" -> Mint; else -> Lilac }
    AnimatedVisibility(visible = true, exit = shrinkVertically() + fadeOut()) {
        Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF221B42)), shape = RoundedCornerShape(14.dp)) {
            Row(Modifier.fillMaxWidth().height(androidx.compose.foundation.layout.IntrinsicSize.Min),
                verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.width(5.dp).fillMaxHeight().background(bar))
                Checkbox(checked = done, onCheckedChange = { done = it },
                    colors = CheckboxDefaults.colors(checkedColor = Mint, uncheckedColor = Lilac, checkmarkColor = Ink))
                Column(Modifier.weight(1f).padding(vertical = 10.dp)) {
                    Text(t.title, color = strike, textDecoration = if (done) TextDecoration.LineThrough else null)
                    val due = t.due?.let { d -> if (t.hasTime) d.replace("T", " ") else d.substringBefore("T") }
                    val details = listOfNotNull(due?.let { "due $it" }, t.remindAt?.let { "⏰ ${it.replace("T", " ")}" })
                    if (details.isNotEmpty()) Text(details.joinToString(" · "), fontSize = 12.sp,
                        color = if (t.overdue) Pink else Muted)
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Swipe(onDelete: () -> Unit, content: @Composable () -> Unit) {
    val state = rememberSwipeToDismissBoxState(confirmValueChange = {
        if (it == SwipeToDismissBoxValue.EndToStart) { onDelete(); true } else false
    })
    SwipeToDismissBox(state, enableDismissFromStartToEnd = false, backgroundContent = {
        Box(Modifier.fillMaxSize().clip(RoundedCornerShape(14.dp)).background(Color(0xFF5A2035)).padding(end = 20.dp),
            contentAlignment = Alignment.CenterEnd) { Text("DELETE", color = Color.White, fontSize = 12.sp) }
    }) { content() }
}

@Composable
private fun Empty(title: String, hint: String) {
    Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        val t = rememberInfiniteTransition(label = "empty")
        val y by t.animateFloat(-6f, 6f, infiniteRepeatable(tween(1400), RepeatMode.Reverse), label = "y")
        Text("🗓", fontSize = 46.sp, modifier = Modifier.graphicsLayer { translationY = y })
        Spacer(Modifier.height(10.dp))
        Text(title, color = Color.White, fontSize = 18.sp)
        Text(hint, color = Muted, fontSize = 13.sp, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
    }
}

/** Add a task or an event. */
@Composable
private fun AddDialog(startWithEvent: Boolean, close: () -> Unit) {
    val context = LocalContext.current
    var event by remember { mutableStateOf(startWithEvent) }
    var title by remember { mutableStateOf("") }
    var priority by remember { mutableStateOf("medium") }
    var date by remember { mutableStateOf<LocalDate?>(if (startWithEvent) LocalDate.now() else null) }
    var time by remember { mutableStateOf<java.time.LocalTime?>(null) }
    var reminder by remember { mutableStateOf<Int?>(10) }
    fun pickDate() {
        val d = date ?: LocalDate.now()
        DatePickerDialog(context, { _, y, m, day -> date = LocalDate.of(y, m + 1, day) }, d.year, d.monthValue - 1,
            d.dayOfMonth).show()
    }
    fun pickTime() {
        val t = time ?: java.time.LocalTime.now().plusHours(1).withMinute(0)
        TimePickerDialog(context, { _, h, min -> time = java.time.LocalTime.of(h, min) }, t.hour, t.minute, true).show()
    }
    AlertDialog(onDismissRequest = close, containerColor = Color(0xFF221B42),
        title = { Text(if (event) "New event" else "New task", color = Color.White) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(selected = !event, onClick = { event = false }, label = { Text("Task") })
                    FilterChip(selected = event, onClick = { event = true; if (date == null) date = LocalDate.now() },
                        label = { Text("Event") })
                }
                OutlinedTextField(title, { title = it }, label = { Text("Title") }, singleLine = true)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { pickDate() }) { Text(date?.let { dayLabel(it) } ?: "Date") }
                    OutlinedButton(onClick = { pickTime() }) { Text(time?.toString() ?: "Time") }
                }
                if (event) {
                    Text("Remind me", color = Muted, fontSize = 12.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf(null to "Off", 0 to "On time", 10 to "10 min", 30 to "30 min").forEach { (v, l) ->
                            FilterChip(selected = reminder == v, onClick = { reminder = v }, label = { Text(l, fontSize = 12.sp) })
                        }
                    }
                } else {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf("low", "medium", "high").forEach { p ->
                            FilterChip(selected = priority == p, onClick = { priority = p }, label = { Text(p) })
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = {
                if (title.isBlank()) { Toast.makeText(context, "Give it a title", Toast.LENGTH_SHORT).show(); return@TextButton }
                if (event) {
                    val d = date ?: LocalDate.now()
                    val t = time ?: run { Toast.makeText(context, "Pick a time", Toast.LENGTH_SHORT).show(); return@TextButton }
                    Bridge.addEvent(title.trim(), LocalDateTime.of(d, t).toString(), reminder)
                } else {
                    val due = date?.let { d -> if (time != null) LocalDateTime.of(d, time).toString() else d.toString() }
                    Bridge.addTask(title.trim(), priority, due)
                }
                close()
            }) { Text("ADD", color = Lilac) }
        },
        dismissButton = { TextButton(onClick = close) { Text("CANCEL", color = Muted) } })
}
